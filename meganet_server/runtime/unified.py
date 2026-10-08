"""Unified TCP auto-detection and dispatch for MegaNet/Telebradesco."""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from ..common import _close_client_writer
from ..meganet.protocol import CR, LF
from ..telebradesco.adapter import (
    TELEBRADESCO_ADAPTER_REQUEST_MAGIC, handle_telebradesco_adapter_client,
)
from .configuration import service_args as _service_args
from .transport import handle_client

MEGANET_AUTO_PROBE_REQUEST_MAGIC = b"MGA1"
MEGANET_AUTO_READY_MAGIC = b"MGR1"

class _PrependAsyncReader:
    """Async reader that replays bytes consumed during TCP auto-detection."""

    def __init__(self, initial: bytes, reader: object) -> None:
        self._buffer = bytearray(initial or b"")
        self._reader = reader

    async def read(self, n: int = 4096) -> bytes:
        if self._buffer:
            if n is None or n < 0:
                data = bytes(self._buffer)
                self._buffer.clear()
                return data
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
            return data
        return await self._reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("readexactly size must be non-negative")
        if n == 0:
            return b""
        chunks = bytearray()
        if self._buffer:
            take = min(n, len(self._buffer))
            chunks.extend(self._buffer[:take])
            del self._buffer[:take]
        remaining = n - len(chunks)
        if remaining:
            try:
                chunks.extend(await self._reader.readexactly(remaining))
            except asyncio.IncompleteReadError as exc:
                chunks.extend(exc.partial)
                raise asyncio.IncompleteReadError(bytes(chunks), n) from None
        return bytes(chunks)

    def __getattr__(self, name: str):
        return getattr(self._reader, name)


def _unified_tcp_find_corrupt_preamble(buffer: bytes) -> Optional[tuple[int, int]]:
    """Find bytes that cannot begin either supported service.

    Before dispatch, both validated protocols begin with textual Hayes/data
    phase bytes.  High-bit bytes and controls other than NUL/TAB/CR/LF are
    therefore evidence of a corrupted modem connection, not a service marker.
    """
    for offset, value in enumerate(buffer):
        if value in (0x00, 0x09, CR, LF):
            continue
        if 0x20 <= value <= 0x7E:
            continue
        return offset, value
    return None


def _unified_tcp_is_ambiguous_hayes_prefix(buffer: bytes) -> bool:
    """Return True when the shared TCP port has only an incomplete Hayes/AT prefix.

    In the BizHawk/original-hardware style flow the TCP socket can be opened
    before the client-side modem command has fully transitioned to data mode.
    The server may therefore see only the first byte of a Hayes command, most
    commonly 0x41 ("A"), and then hit the detection timeout. Falling back to
    MegaNet at that point is unsafe because MegaNet immediately emits
    ``login:\r`` while Telebradesco must remain silent until the Class-1
    dialogue starts.

    This helper intentionally treats an ambiguous Hayes prefix as Telebradesco
    at the dispatcher level. MegaNet remains detected by explicit tokens or by
    the confirmed ``ATX3DP`` dial path before this fallback is considered.
    """
    if not buffer:
        return False
    text = buffer.decode("latin-1", "ignore").replace("\x00", "")
    upper = text.upper().strip()
    if not upper:
        return False
    # Strong MegaNet markers must keep precedence over any generic AT/Hayes
    # interpretation.
    if any(marker in upper for marker in (
        "#ID ", "#GET MAIL#", "#BEGIN MAIL#", "#SEND MAIL#", "#SEND NEWS",
        "#CADASTRO", "ATX3DP", "MEGANET", "MEGA NET",
    )):
        return False
    # A single 'A', 'AT', or a partial AT command is the exact problematic
    # case observed as RX 41.  It should not trigger the MegaNet fallback.
    if upper == "A" or upper == "AT" or upper.startswith("AT"):
        return True
    return False


def _unified_tcp_is_meganet_empty_cr_signal(buffer: bytes) -> bool:
    """Recognize the validated MegaNet data-phase wake-up on a shared port.

    In the MegaNet BizHawk/bridge flow the socket is opened before the ROM
    reaches online mode and the first byte visible to the backend is a lone CR.
    Waiting for the whole unified-detection timeout after that CR adds needless
    latency. Telebradesco has been observed to begin with an AT/Class-1 prefix
    (or the binary TBA1 adapter marker), not a bare CR. Keep this deliberately
    narrow: exactly one CR, or CR followed only by LF/NUL idle bytes.
    """
    if not buffer or CR not in buffer:
        return False
    if buffer[0] != CR:
        return False
    return all(value in (CR, LF, 0x00) for value in buffer)


def _unified_tcp_detect_service(buffer: bytes) -> Optional[str]:
    """Infer service from the first bytes of a raw client TCP session.

    This intentionally does not depend on any emulator preamble. It only uses
    tokens and commands that are part of the original cartridge/modem dialogue.
    Ambiguous modem noise such as CR/LF/NUL/OK is left undecided so the caller
    can continue waiting or use the configured fallback.
    """
    if not buffer:
        return None

    # app_meganet/app_telebradesco internal adapter request.  This marker is
    # protocol-owned and is checked before Hayes/application heuristics so the
    # same TCP listener can safely serve physical MegaNet and Telebradesco calls.
    if len(buffer) >= len(TELEBRADESCO_ADAPTER_REQUEST_MAGIC) and buffer.startswith(TELEBRADESCO_ADAPTER_REQUEST_MAGIC):
        return "telebradesco-adapter"

    # Provisional MegaNet probe from app_meganet auto mode. The marker is
    # consumed by the unified dispatcher and never reaches the legacy MegaNet
    # bootstrap parser. It lets the server emit login prompts while Asterisk
    # waits for a genuinely decoded V.22 byte.
    if len(buffer) >= len(MEGANET_AUTO_PROBE_REQUEST_MAGIC) and buffer.startswith(MEGANET_AUTO_PROBE_REQUEST_MAGIC):
        return "meganet-auto-probe"

    text = buffer.decode("latin-1", "ignore")
    upper = text.upper()
    stripped = upper.replace("\x00", "").strip()

    # Telebradesco confirmed Class-1 fax/modem flow. These commands are strong
    # indicators because MegaNet's application flow is textual after connect.
    tele_markers = (
        # Handshake Hayes/Class-1 observado no Telebradesco antes do fluxo
        # binário/fax propriamente dito. Em porta unificada, estes comandos
        # aparecem antes de AT+FRH/AT+FTH, portanto precisam vencer o fallback
        # MegaNet.
        "AT&FV0B0+FCLASS=1", "+FCLASS=1", "+FCLASS", "+FF=1",
        "AT+FRH=3", "AT+FTH=3", "AT+FRM=48", "AT+FTM=", "AT+F",
        "MEGABRA",
    )
    if any(marker in upper for marker in tele_markers):
        return "telebradesco"

    # Discagem observada no Telebradesco: ATX3DT,,777777. No MegaNet
    # validado até aqui, a discagem local usa ATX3DP,<número>. A distinção
    # D*T* evita que o dispatcher envie a sessão Telebradesco para MegaNet.
    if "ATX3DT" in upper:
        return "telebradesco"

    # MegaNet confirmed application tokens and textual login/cadastro traffic.
    mega_markers = (
        "#ID ", "#GET MAIL#", "#BEGIN MAIL#", "#SEND MAIL#", "#SEND NEWS",
        "#CADASTRO", "#BEMVINDO#", "MEGANET", "MEGA NET",
    )
    if any(marker in upper for marker in mega_markers):
        return "meganet"

    # MegaNet local-modem path observed with ATX3DP,<number>. Generic ATD is
    # deliberately not enough because it is a modem command, not an application
    # identity. Do not classify ATX3DT here: it is used by the Telebradesco
    # flow observed with the original cartridge/emulator path.
    if "ATX3DP" in upper:
        return "meganet"

    # Do NOT classify arbitrary printable text too early. In unified TCP mode
    # the first byte observed from Telebradesco can be just 0x41 ("A"), the
    # beginning of an AT command. Older detection treated a single non-"AT"
    # alphanumeric byte as MegaNet and dispatched before +FCLASS=1 or ATX3DT
    # could arrive. MegaNet remains detectable by explicit #tokens, by its
    # ATX3DP dial path, or by the configured fallback after the detection
    # window expires.

    return None


async def _wait_meganet_auto_ready(
    reader: asyncio.StreamReader,
    peer: str,
    timeout: float,
    initial: bytes = b"",
) -> tuple[bytes, bool, bool]:
    """Wait strictly for MGR1 after MGA1, preserving bytes and reporting peer close."""
    buffered = bytearray(initial)
    peer_closed = False
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout)
    while len(buffered) < 512:
        marker_at = buffered.find(MEGANET_AUTO_READY_MAGIC)
        if marker_at >= 0:
            replay = bytes(buffered[:marker_at] + buffered[marker_at + len(MEGANET_AUTO_READY_MAGIC):])
            logging.info(
                "%s TCP_UNIFIED_MEGANET_AUTO_READY magic=MGR1 consumed=%d "
                "preserved=%s action=start-online-login",
                peer, len(MEGANET_AUTO_READY_MAGIC), replay.hex(" ") or "-",
            )
            return replay, True, False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(1), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not chunk:
            peer_closed = True
            break
        buffered.extend(chunk)
    if not peer_closed:
        logging.warning(
            "%s TCP_UNIFIED_MEGANET_AUTO_READY_TIMEOUT timeout=%.2fs buffered=%s "
            "action=close-without-login",
            peer, timeout, bytes(buffered).hex(" ") or "-",
        )
    return bytes(buffered), False, peer_closed


async def handle_unified_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "unknown"
    timeout = max(0.2, float(getattr(args, "unified_tcp_detect_timeout", 3.0) or 3.0))
    fallback = str(getattr(args, "unified_tcp_default_service", getattr(args, "active_service", "meganet")) or "meganet").strip().lower()
    if fallback not in {"meganet", "telebradesco"}:
        fallback = "meganet"
    buffer = bytearray()
    detected: Optional[str] = None
    early_dispatch_reason: Optional[str] = None
    peer_closed_without_data = False
    corrupt_preamble: Optional[tuple[int, int]] = None
    deadline = asyncio.get_running_loop().time() + timeout

    logging.info("%s TCP_UNIFIED_ACCEPT timeout=%.2fs fallback=%s", peer, timeout, fallback)
    while detected is None and len(buffer) < 512:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(1), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not chunk:
            if not buffer:
                peer_closed_without_data = True
            break
        buffer.extend(chunk)
        corrupt_preamble = _unified_tcp_find_corrupt_preamble(bytes(buffer))
        if corrupt_preamble:
            break
        detected = _unified_tcp_detect_service(bytes(buffer))
        if detected:
            break
        if fallback == "meganet" and _unified_tcp_is_meganet_empty_cr_signal(bytes(buffer)):
            detected = "meganet"
            early_dispatch_reason = "empty-cr-data-phase"
            logging.info(
                "%s TCP_UNIFIED_MEGANET_EMPTY_CR action=dispatch-immediate buffered=%s",
                peer, bytes(buffer).hex(" ") or "-",
            )
            break
        # Once a full AT command is present and it still is not a Telebradesco
        # Class-1 command, let the fallback decide rather than misclassifying.
        if chunk in (b"\r", b"\n") and buffer.strip() and len(buffer) >= 4:
            maybe = _unified_tcp_detect_service(bytes(buffer))
            if maybe:
                detected = maybe
                break

    if peer_closed_without_data:
        logging.info(
            "%s TCP_UNIFIED_EMPTY_PROBE_CLOSED action=close-without-dispatch",
            peer,
        )
        await _close_client_writer(writer, peer, "TCP unified empty auto probe")
        return

    if corrupt_preamble:
        offset, bad_byte = corrupt_preamble
        logging.warning(
            "%s TCP_UNIFIED_REJECT_CORRUPT_PREAMBLE offset=%d byte=%02X "
            "buffered=%s action=close-without-dispatch",
            peer,
            offset,
            bad_byte,
            bytes(buffer[:128]).hex(" ") or "-",
        )
        await _close_client_writer(writer, peer, "TCP unified corrupt preamble")
        return

    dispatch_reason = early_dispatch_reason or ("detected" if detected else "fallback")
    if detected is None and _unified_tcp_is_ambiguous_hayes_prefix(bytes(buffer)):
        # With no emulator preamble and a single shared TCP port, an incomplete
        # Hayes/AT prefix cannot be safely interpreted as MegaNet.  Telebradesco
        # is the protocol that must remain silent after CONNECT; sending the
        # MegaNet login prompt here breaks the ROM before it can send AT+FRH=3.
        service = "telebradesco"
        dispatch_reason = "ambiguous-hayes-prefix"
    else:
        service = detected or fallback
    logging.info(
        "%s TCP_UNIFIED_DISPATCH service=%s reason=%s detected=%s buffered=%s",
        peer, service, dispatch_reason, detected or "-", bytes(buffer).hex(" ") or "-",
    )
    if service == "telebradesco-adapter":
        replay_reader = _PrependAsyncReader(bytes(buffer), reader)
        await handle_telebradesco_adapter_client(replay_reader, writer, args)
        return
    if service == "meganet-auto-probe":
        remainder = bytes(buffer[len(MEGANET_AUTO_PROBE_REQUEST_MAGIC):])
        ready_timeout = max(0.5, min(30.0, float(
            getattr(args, "meganet_auto_ready_timeout", 12.0) or 12.0
        )))
        logging.info(
            "%s TCP_UNIFIED_MEGANET_AUTO_PROBE magic=MGA1 consumed=%d remainder=%s "
            "action=wait-MGR1 timeout=%.2fs",
            peer, len(MEGANET_AUTO_PROBE_REQUEST_MAGIC), remainder.hex(" ") or "-",
            ready_timeout,
        )
        replay, ready_seen, peer_closed = await _wait_meganet_auto_ready(
            reader, peer, ready_timeout, remainder
        )
        # MGA1 is an explicit modern app_meganet reservation.  Once
        # it was received, compatibility fallback is unsafe: lack of MGR1 means
        # the modem never proved a stable 2400-bps data phase.  Close silently
        # instead of entering online_start and emitting login: prompts.
        if not ready_seen:
            if peer_closed:
                logging.info(
                    "%s TCP_UNIFIED_MEGANET_AUTO_PROBE_CLOSED_BEFORE_READY "
                    "buffered=%s action=close-without-login",
                    peer, replay.hex(" ") or "-",
                )
                close_reason = "MegaNet auto probe closed before MGR1"
            else:
                # _wait_meganet_auto_ready() already logged the strict timeout
                # with action=close-without-login.  Keep a single authoritative
                # timeout event and only perform the close here.
                close_reason = "MegaNet MGA1 timeout waiting for MGR1"
            await _close_client_writer(writer, peer, close_reason)
            return

        replay_reader = _PrependAsyncReader(replay, reader)
        service_args = _service_args(args, "meganet")
        service_args.online_start = True
        service_args.meganet_auto_ready_seen = True
        await handle_client(replay_reader, writer, service_args)
        return
    replay_reader = _PrependAsyncReader(bytes(buffer), reader)
    service_args = _service_args(args, service)
    await handle_client(replay_reader, writer, service_args)
