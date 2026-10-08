from __future__ import annotations

import asyncio
from dataclasses import dataclass

from meganet_server import api
from meganet_server.meganet import protocol
from meganet_server.meganet import transport
from meganet_server.meganet.states import MegaNetState


def _client_frame(seq: int, payload: bytes) -> bytes:
    checksum = (len(payload) + seq + sum(payload)) & 0xFFFF
    return (
        bytes([protocol.STX, len(payload), seq])
        + payload
        + bytes([checksum & 0xFF, checksum >> 8, protocol.ETX])
    )


class QueueReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()

    async def read(self, _n: int = 4096) -> bytes:
        item = await self.queue.get()
        if item is EOF:
            return b""
        if isinstance(item, BaseException):
            raise item
        return bytes(item)

    def feed(self, data: bytes) -> None:
        self.queue.put_nowait(bytes(data))

    def feed_eof(self) -> None:
        self.queue.put_nowait(EOF)

    def feed_error(self, exc: BaseException) -> None:
        self.queue.put_nowait(exc)


EOF = object()


class CaptureStreamWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self.write_event = asyncio.Event()
        self.wait_closed_calls = 0

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 24567)
        return default

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self.write_event.set()

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True
        self.write_event.set()

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed


class FailingDrainWriter(CaptureStreamWriter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_on_framed_write = True

    async def drain(self) -> None:
        if self.fail_on_framed_write and self.writes and self.writes[-1].startswith(bytes([protocol.STX])):
            raise ConnectionResetError("simulated carrier loss")
        await asyncio.sleep(0)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.001)


@dataclass
class RunningTransport:
    task: asyncio.Task
    reader: QueueReader
    writer: CaptureStreamWriter
    session: object


async def _start_transport(tmp_path, monkeypatch, *, writer=None, retries: int = 2) -> RunningTransport:
    config_path = tmp_path / "meganet_config.json"
    args = api.parse_args(["--config", str(config_path)])
    args.online_start = True
    args.meganet_auto_ready_seen = True
    args.profile_idle = 0.001
    args.post_dle_delay = 0.001
    args.outbound_packet_delay = 0.0
    args.meganet_packet_ack_mode = "all"
    args.meganet_packet_ack_timeout = 0.2
    args.meganet_packet_max_retries = retries
    args.unknown_token_action = "ready"
    args.welcome_email_enabled = False
    args.raw_hayes_in_data_action = "ignore"

    captured: dict[str, object] = {}
    real_session = transport.MegaNetSession

    def session_factory(*factory_args, **factory_kwargs):
        sess = real_session(*factory_args, **factory_kwargs)
        captured["session"] = sess
        return sess

    monkeypatch.setattr(transport, "MegaNetSession", session_factory)
    reader = QueueReader()
    actual_writer = writer or CaptureStreamWriter()
    task = asyncio.create_task(transport.handle_meganet_client(reader, actual_writer, args))
    await _wait_until(lambda: "session" in captured)
    session = captured["session"]
    await _wait_until(lambda: getattr(session, "state", None) == MegaNetState.WAIT_LOGIN)
    return RunningTransport(task=task, reader=reader, writer=actual_writer, session=session)


async def _bootstrap_to_packet(running: RunningTransport) -> None:
    sess = running.session
    reader = running.reader
    reader.feed(b"meganet\r")
    await _wait_until(lambda: sess.state == MegaNetState.WAIT_PASSWORD)
    reader.feed(b"guerta\r")
    await _wait_until(lambda: sess.state == MegaNetState.WAIT_PROFILE_FIELD)
    reader.feed(b"MAURICIO")
    await _wait_until(lambda: sess.state == MegaNetState.WAIT_DLE)
    reader.feed(bytes([protocol.DLE]))
    await _wait_until(lambda: len([item for item in running.writer.writes if item.startswith(bytes([protocol.STX]))]) >= 1)
    reader.feed(bytes([protocol.ACK]))
    await _wait_until(lambda: len([item for item in running.writer.writes if item.startswith(bytes([protocol.STX]))]) >= 2)
    reader.feed(bytes([protocol.ACK]))
    await _wait_until(lambda: sess.state == MegaNetState.PACKET)


async def _wait_for_framed_writes(writer: CaptureStreamWriter, count: int, *, timeout: float = 1.0) -> list[bytes]:
    await _wait_until(
        lambda: len([item for item in writer.writes if item.startswith(bytes([protocol.STX]))]) >= count,
        timeout=timeout,
    )
    return [item for item in writer.writes if item.startswith(bytes([protocol.STX]))]


def test_transport_nak_retransmits_same_frame_then_ack_allows_clean_eof(tmp_path, monkeypatch):
    async def scenario() -> None:
        running = await _start_transport(tmp_path, monkeypatch)
        await _bootstrap_to_packet(running)
        sess = running.session

        running.reader.feed(_client_frame(0, b"#transport probe#\r"))
        first = (await _wait_for_framed_writes(running.writer, 3))[-1]
        # First two framed writes are #bemvindo# and #ready# from bootstrap.
        assert first == protocol.encode_server_frame(2, b"#ready#\r")

        running.reader.feed(bytes([protocol.NAK]))
        framed = await _wait_for_framed_writes(running.writer, 4)
        assert framed[-1] == first
        running.reader.feed(bytes([protocol.ACK]))
        await _wait_until(lambda: sess.packet_tx_confirmed >= 3)

        running.reader.feed_eof()
        await asyncio.wait_for(running.task, timeout=1.0)
        assert running.writer.closed is True
        assert running.writer.wait_closed_calls == 1
        assert sess.packet_delivery_failed is False
        assert sess.packet_tx_retransmissions == 1
        assert sess.packet_tx_naks == 1
        assert sess.server_seq == 3

    asyncio.run(scenario())


def test_transport_eof_while_waiting_ack_aborts_delivery_and_closes(tmp_path, monkeypatch):
    async def scenario() -> None:
        running = await _start_transport(tmp_path, monkeypatch)
        await _bootstrap_to_packet(running)
        sess = running.session

        running.reader.feed(_client_frame(0, b"#transport eof#\r"))
        await _wait_for_framed_writes(running.writer, 3)
        running.reader.feed_eof()
        await asyncio.wait_for(running.task, timeout=1.0)

        assert running.writer.closed is True
        assert sess.packet_delivery_failed is True
        assert sess.packet_tx_failures == 1
        assert sess.packet_tx_attempts == 3  # bootstrap #bemvindo#, #ready#, probe #ready#
        assert sess.packet_tx_confirmed == 2
        assert sess.state is MegaNetState.AUTH_REJECTED
        assert "client EOF" in sess.packet_reader_close_reason

    asyncio.run(scenario())


def test_transport_ack_timeout_exhausts_retries_and_cancels_reader(tmp_path, monkeypatch):
    async def scenario() -> None:
        running = await _start_transport(tmp_path, monkeypatch, retries=2)
        await _bootstrap_to_packet(running)
        sess = running.session

        running.reader.feed(_client_frame(0, b"#transport timeout#\r"))
        # Do not send ACK/NAK. The transport must retry and then fail closed.
        await asyncio.wait_for(running.task, timeout=1.5)

        framed = [item for item in running.writer.writes if item.startswith(bytes([protocol.STX]))]
        probe_frames = [item for item in framed if item == protocol.encode_server_frame(2, b"#ready#\r")]
        assert len(probe_frames) == 2
        assert running.writer.closed is True
        assert sess.packet_delivery_failed is True
        assert sess.packet_tx_timeouts == 2
        assert sess.packet_tx_retransmissions == 1
        assert sess.packet_tx_failures == 1
        assert sess.server_seq == 2
        assert sess.state is MegaNetState.AUTH_REJECTED

    asyncio.run(scenario())


def test_transport_writer_drain_failure_aborts_immediately(tmp_path, monkeypatch):
    async def scenario() -> None:
        writer = FailingDrainWriter()
        # Permit bootstrap frames; fail only after bootstrap is complete.
        writer.fail_on_framed_write = False
        running = await _start_transport(tmp_path, monkeypatch, writer=writer)
        await _bootstrap_to_packet(running)
        sess = running.session
        writer.fail_on_framed_write = True

        running.reader.feed(_client_frame(0, b"#transport carrier#\r"))
        await asyncio.wait_for(running.task, timeout=1.0)

        assert writer.closed is True
        assert sess.packet_delivery_failed is True
        assert sess.packet_tx_failures == 1
        assert sess.packet_tx_timeouts == 0
        assert sess.state is MegaNetState.AUTH_REJECTED

    asyncio.run(scenario())


def test_socket_reader_connection_error_notifies_session_and_terminates_queue(tmp_path):
    async def scenario() -> None:
        class MinimalSession:
            peer = "reader-error"

            def __init__(self) -> None:
                self.routed: list[bytes] = []
                self.flushed = False
                self.closed_reason = ""

            def route_inbound_wire_data(self, data, queue):
                self.routed.append(bytes(data))
                queue.put_nowait(bytes(data))

            def flush_packet_wire_buffer(self, _queue):
                self.flushed = True

            def notify_packet_reader_closed(self, reason):
                self.closed_reason = reason

        reader = QueueReader()
        session = MinimalSession()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        task = asyncio.create_task(transport._meganet_socket_reader_loop(reader, session, queue))
        reader.feed(b"abc")
        reader.feed_error(ConnectionResetError("carrier lost"))
        await asyncio.wait_for(task, timeout=1.0)
        assert session.routed == [b"abc"]
        assert session.flushed is True
        assert "reader error" in session.closed_reason
        assert await queue.get() == b"abc"
        assert await queue.get() is None

    asyncio.run(scenario())


def test_arq_demux_preserves_client_frame_with_ack_nak_inside_payload(tmp_path):
    async def scenario() -> None:
        from meganet_server.meganet import content
        from meganet_server.meganet.session import MegaNetSession

        writer = CaptureStreamWriter()
        sess = MegaNetSession(
            writer,
            peer="demux-frame",
            message_lines=content.DEFAULT_MESSAGE,
            output_dir=tmp_path / "sessions",
            cadastro_db_path=tmp_path / "cadastros.json",
            online_start=False,
        )
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        payload = b"A" + bytes([protocol.ACK]) + b"B" + bytes([protocol.NAK]) + b"C"
        frame = _client_frame(9, payload)
        sess.packet_ack_waiting_seq = 4

        sess.route_inbound_wire_data(frame[:1], queue)
        assert queue.empty()
        sess.route_inbound_wire_data(frame[1:], queue)

        assert await queue.get() == frame
        assert sess.packet_ack_queue.empty()

    asyncio.run(scenario())


def test_arq_demux_splits_raw_line_control_and_client_frame(tmp_path):
    async def scenario() -> None:
        from meganet_server.meganet import content
        from meganet_server.meganet.session import MegaNetSession

        writer = CaptureStreamWriter()
        sess = MegaNetSession(
            writer,
            peer="demux-mixed",
            message_lines=content.DEFAULT_MESSAGE,
            output_dir=tmp_path / "sessions",
            cadastro_db_path=tmp_path / "cadastros.json",
            online_start=False,
        )
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        frame = _client_frame(2, b"#bye#\r")
        sess.packet_ack_waiting_seq = 7
        sess.route_inbound_wire_data(b"diagnostic\r" + bytes([protocol.ACK]) + frame, queue)

        assert await queue.get() == b"diagnostic\r"
        assert await queue.get() == frame
        control, received_at = sess.packet_ack_queue.get_nowait()
        assert control == protocol.ACK
        assert received_at > 0

    asyncio.run(scenario())


def test_arq_flushes_partial_wire_buffer_on_reader_close(tmp_path):
    async def scenario() -> None:
        from meganet_server.meganet import content
        from meganet_server.meganet.session import MegaNetSession

        writer = CaptureStreamWriter()
        sess = MegaNetSession(
            writer,
            peer="demux-flush",
            message_lines=content.DEFAULT_MESSAGE,
            output_dir=tmp_path / "sessions",
            cadastro_db_path=tmp_path / "cadastros.json",
            online_start=False,
        )
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        sess.packet_ack_waiting_seq = 3
        sess.route_inbound_wire_data(b"partial-without-cr", queue)
        assert queue.empty()
        sess.flush_packet_wire_buffer(queue)
        assert await queue.get() == b"partial-without-cr"
        assert sess.packet_wire_buffer == bytearray()

    asyncio.run(scenario())
