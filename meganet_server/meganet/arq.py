"""Reliable MegaNet server-to-ROM delivery (ACK/NAK stop-and-wait).

The mixin intentionally operates on the session attributes initialized by
``MegaNetSession`` so the validated timing and retry semantics remain unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .protocol import ACK, NAK, STX, CR, LF, display_payload, encode_server_frame
from .states import MegaNetState

class MegaNetPacketDeliveryError(ConnectionError):
    """Raised when a server-to-ROM MegaNet frame cannot be confirmed."""

class MegaNetArqMixin:
    def _writer_is_closing(self) -> bool:
            checker = getattr(self.writer, "is_closing", None)
            if callable(checker):
                try:
                    return bool(checker())
                except Exception:
                    return False
            return False

    def route_inbound_wire_data(
            self,
            data: bytes,
            protocol_queue: "asyncio.Queue[Optional[bytes]]",
        ) -> None:
            """Separate raw ACK/NAK controls from complete client frames.

            ACK and NAK are removed only while a server frame is outstanding.
            Client frames beginning with STX are preserved byte-for-byte, including
            any 0x06/0x15 that occurs inside payload or checksum.
            """
            if not data:
                return

            if self.packet_ack_waiting_seq is None and not self.packet_wire_buffer:
                protocol_queue.put_nowait(bytes(data))
                return

            self.packet_wire_buffer.extend(data)

            while self.packet_wire_buffer:
                first = self.packet_wire_buffer[0]

                if first in (ACK, NAK):
                    del self.packet_wire_buffer[0]
                    if self.packet_ack_waiting_seq is not None:
                        received_at = asyncio.get_running_loop().time()
                        self.packet_ack_queue.put_nowait((first, received_at))
                        logging.info(
                            "%s CLIENT_PACKET_CONTROL control=%s pending_seq=%d",
                            self.peer,
                            "ACK" if first == ACK else "NAK",
                            self.packet_ack_waiting_seq,
                        )
                    else:
                        protocol_queue.put_nowait(bytes([first]))
                    continue

                if first == STX:
                    if len(self.packet_wire_buffer) < 2:
                        break
                    length = self.packet_wire_buffer[1] & 0x7F
                    total = 1 + 1 + 1 + length + 2 + 1
                    if len(self.packet_wire_buffer) < total:
                        break
                    frame = bytes(self.packet_wire_buffer[:total])
                    del self.packet_wire_buffer[:total]
                    protocol_queue.put_nowait(frame)
                    continue

                marker_positions = [
                    pos
                    for marker in (ACK, NAK, STX)
                    if (pos := self.packet_wire_buffer.find(bytes([marker]), 1)) >= 0
                ]
                if marker_positions:
                    boundary = min(marker_positions)
                    raw = bytes(self.packet_wire_buffer[:boundary])
                    del self.packet_wire_buffer[:boundary]
                    if raw:
                        protocol_queue.put_nowait(raw)
                    continue

                cr_pos = self.packet_wire_buffer.rfind(bytes([CR]))
                lf_pos = self.packet_wire_buffer.rfind(bytes([LF]))
                line_end = max(cr_pos, lf_pos)
                if line_end >= 0:
                    raw = bytes(self.packet_wire_buffer[:line_end + 1])
                    del self.packet_wire_buffer[:line_end + 1]
                    protocol_queue.put_nowait(raw)
                    continue

                if len(self.packet_wire_buffer) > 256:
                    raw = bytes(self.packet_wire_buffer[:-64])
                    del self.packet_wire_buffer[:-64]
                    if raw:
                        protocol_queue.put_nowait(raw)
                    continue
                break

    def flush_packet_wire_buffer(
            self,
            protocol_queue: "asyncio.Queue[Optional[bytes]]",
        ) -> None:
            if self.packet_wire_buffer:
                protocol_queue.put_nowait(bytes(self.packet_wire_buffer))
                self.packet_wire_buffer.clear()

    def notify_packet_reader_closed(self, reason: str) -> None:
            if self.packet_reader_closed:
                return
            self.packet_reader_closed = True
            self.packet_reader_close_reason = str(reason or "reader closed")
            self.packet_ack_queue.put_nowait(None)

    def _drain_stale_packet_controls(self) -> int:
            drained = 0
            while True:
                try:
                    item = self.packet_ack_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    self.packet_reader_closed = True
                else:
                    drained += 1
            if drained:
                logging.info(
                    "%s CLIENT_PACKET_CONTROL_STALE drained=%d action=discard-before-new-frame",
                    self.peer,
                    drained,
                )
            return drained

    def _close_after_packet_delivery_failure(
            self,
            *,
            seq: int,
            reason: str,
        ) -> MegaNetPacketDeliveryError:
            self.packet_delivery_failed = True
            self.packet_tx_failures += 1
            self.closed_by_transaction = True
            self.state = MegaNetState.AUTH_REJECTED
            self.cancel_login_prompt_repeater()
            self.cancel_bootstrap_watchdog()
            if self.profile_idle_task and not self.profile_idle_task.done():
                self.profile_idle_task.cancel()
            if self.post_dle_task and not self.post_dle_task.done():
                current = asyncio.current_task()
                if self.post_dle_task is not current:
                    self.post_dle_task.cancel()
            if self.close_task and not self.close_task.done():
                self.close_task.cancel()
            logging.error(
                "%s SERVER_PACKET_DELIVERY_FAILED seq=%d attempts=%d timeout=%.3fs "
                "reason=%s action=close-tcp",
                self.peer,
                seq,
                self.meganet_packet_max_retries,
                self.meganet_packet_ack_timeout_seconds,
                reason,
            )
            try:
                self.writer.close()
            except Exception:
                pass
            return MegaNetPacketDeliveryError(
                f"MegaNet packet seq={seq} delivery failed: {reason}"
            )

    async def send_packet_reliable(self, payload: bytes, reason: str) -> None:
            """Send one frame and advance SEQ only after raw ACK 0x06."""
            async with self.outbound_packet_lock:
                seq = self.server_seq
                frame = encode_server_frame(seq, payload)
                loop = asyncio.get_running_loop()

                for attempt in range(1, self.meganet_packet_max_retries + 1):
                    self._drain_stale_packet_controls()

                    if self.packet_reader_closed or self._writer_is_closing():
                        raise self._close_after_packet_delivery_failure(
                            seq=seq,
                            reason=self.packet_reader_close_reason or "transport already closed",
                        )

                    self.packet_ack_waiting_seq = seq
                    self.packet_tx_attempts += 1
                    if attempt > 1:
                        self.packet_tx_retransmissions += 1

                    logging.info(
                        "%s SERVER_PACKET_TX seq=%d len=%d attempt=%d/%d "
                        "framing=HI_LO_NO_ETX reliable=yes reason=%s payload=%s",
                        self.peer,
                        seq,
                        len(payload),
                        attempt,
                        self.meganet_packet_max_retries,
                        reason,
                        display_payload(payload),
                    )

                    try:
                        self.writer.write(frame)
                        sent_at = loop.time()
                        await self.writer.drain()
                    except (ConnectionError, OSError) as exc:
                        self.packet_ack_waiting_seq = None
                        raise self._close_after_packet_delivery_failure(
                            seq=seq,
                            reason=f"write/drain failed: {exc}",
                        ) from exc

                    response: Optional[int] = None
                    response_name = ""
                    try:
                        deadline = loop.time() + self.meganet_packet_ack_timeout_seconds
                        while True:
                            remaining = deadline - loop.time()
                            if remaining <= 0:
                                raise asyncio.TimeoutError
                            event = await asyncio.wait_for(
                                self.packet_ack_queue.get(),
                                timeout=remaining,
                            )
                            if event is None:
                                raise self._close_after_packet_delivery_failure(
                                    seq=seq,
                                    reason=self.packet_reader_close_reason or "client disconnected while awaiting ACK",
                                )
                            control, received_at = event
                            if float(received_at) < sent_at:
                                logging.info(
                                    "%s CLIENT_PACKET_CONTROL_STALE control=%s seq=%d action=ignore",
                                    self.peer,
                                    "ACK" if control == ACK else "NAK",
                                    seq,
                                )
                                continue
                            response = int(control)
                            response_name = "ACK" if response == ACK else "NAK"
                            break
                    except asyncio.TimeoutError:
                        self.packet_tx_timeouts += 1
                        logging.warning(
                            "%s SERVER_PACKET_ACK_TIMEOUT seq=%d attempt=%d/%d "
                            "timeout=%.3fs action=%s",
                            self.peer,
                            seq,
                            attempt,
                            self.meganet_packet_max_retries,
                            self.meganet_packet_ack_timeout_seconds,
                            "retransmit" if attempt < self.meganet_packet_max_retries else "close",
                        )
                    finally:
                        self.packet_ack_waiting_seq = None

                    if response == ACK:
                        self.server_seq = (seq + 1) & 0xFF
                        self.packet_tx_confirmed += 1
                        logging.info(
                            "%s SERVER_PACKET_CONFIRMED seq=%d attempt=%d next_seq=%d",
                            self.peer,
                            seq,
                            attempt,
                            self.server_seq,
                        )
                        return

                    if response == NAK:
                        self.packet_tx_naks += 1
                        logging.warning(
                            "%s SERVER_PACKET_NAK seq=%d attempt=%d/%d action=%s",
                            self.peer,
                            seq,
                            attempt,
                            self.meganet_packet_max_retries,
                            "retransmit" if attempt < self.meganet_packet_max_retries else "close",
                        )

                    if attempt < self.meganet_packet_max_retries:
                        if self.meganet_packet_retry_delay_seconds > 0:
                            await asyncio.sleep(self.meganet_packet_retry_delay_seconds)
                        logging.info(
                            "%s SERVER_PACKET_RETRANSMIT seq=%d next_attempt=%d last_response=%s",
                            self.peer,
                            seq,
                            attempt + 1,
                            response_name or "TIMEOUT",
                        )

                raise self._close_after_packet_delivery_failure(
                    seq=seq,
                    reason="ACK not received after retries",
                )

    async def send_packet(
            self,
            payload: bytes,
            reason: str,
            *,
            reliable: bool = False,
            wait_ack: Optional[bool] = None,
        ) -> None:
            # wait_ack provides a protocol-specific override.  Terminal error
            # transactions are not acknowledged by the ROM with the same cadence as
            # normal downloads; forcing stop-and-wait there can add several
            # ACK-timeout cycles before the ROM displays the error.
            if wait_ack is None:
                require_ack = (
                    self.meganet_packet_ack_mode == "all"
                    or (self.meganet_packet_ack_mode == "transfers" and reliable)
                )
            else:
                require_ack = bool(wait_ack)
            if require_ack:
                await self.send_packet_reliable(payload, reason)
                return

            async with self.outbound_packet_lock:
                seq = self.server_seq
                frame = encode_server_frame(seq, payload)
                logging.info(
                    "%s SERVER_PACKET seq=%d len=%d framing=HI_LO_NO_ETX "
                    "reliable=no reason=%s payload=%s",
                    self.peer,
                    seq,
                    len(payload),
                    reason,
                    display_payload(payload),
                )
                await self.send_raw(frame, f"packet: {reason}")
                self.server_seq = (seq + 1) & 0xFF

    async def send_raw(self, data: bytes, reason: str) -> None:
            logging.debug("%s TX %s reason=%s", self.peer, data.hex(" "), reason)
            self.writer.write(data)
            await self.writer.drain()

