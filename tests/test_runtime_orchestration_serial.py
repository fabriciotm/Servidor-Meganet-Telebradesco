from __future__ import annotations

import argparse
import asyncio
import sys
import types
from pathlib import Path

import pytest

from meganet_server.config import refresh_app_config
from meganet_server.runtime import configuration as rc
from meganet_server.runtime import orchestration as ro
from meganet_server.runtime import serial as rserial
from meganet_server.runtime import transport as rt


class _AdminServer:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.server_close_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.server_close_calls += 1


class _Sock:
    def __init__(self, address: tuple[str, int]) -> None:
        self._address = address

    def getsockname(self):
        return self._address


class _Server:
    def __init__(self, port: int, *, fail: BaseException | None = None) -> None:
        self.sockets = [_Sock(("127.0.0.1", port))]
        self.fail = fail
        self.closed = False
        self.waited = False

    async def serve_forever(self) -> None:
        if self.fail is not None:
            raise self.fail

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def _service_ns(service: str, mode: str, *, host: str, port: int, serial_port: str) -> argparse.Namespace:
    return argparse.Namespace(
        active_service=service,
        server_transport=mode,
        host=host,
        port=port,
        serial_port=serial_port,
        serial_baud=115200,
    )


def _patch_orchestration_basics(monkeypatch, tmp_path: Path, *, adapter_enabled: bool = False):
    adapter = types.SimpleNamespace(
        enabled=adapter_enabled,
        host="127.0.0.1",
        port=19780,
        log_file=tmp_path / "adapter.jsonl",
    )
    tele = types.SimpleNamespace(
        content_file=tmp_path / "telebradesco" / "accounts_content.json",
        accounts_file=tmp_path / "telebradesco" / "accounts.json",
        adapter=adapter,
    )
    app_config = types.SimpleNamespace(telebradesco=tele)
    monkeypatch.setattr(ro, "typed_config", lambda _args: app_config)
    monkeypatch.setattr(ro, "validate_dual_endpoints", lambda _args: None)
    monkeypatch.setattr(ro, "read_telebradesco_accounts", lambda *_a, **_k: {})
    return app_config


def test_orchestration_single_tcp_starts_and_closes_listener(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path)
        root = argparse.Namespace()
        admin = _AdminServer()
        endpoints = {
            "meganet": _service_ns("meganet", "tcp", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "off", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])
        started: list[tuple[object, str, int]] = []
        server = _Server(19777)

        async def fake_start_server(callback, *, host, port):
            started.append((callback, host, port))
            return server

        monkeypatch.setattr(ro.asyncio, "start_server", fake_start_server)
        dispatched: dict[str, object] = {}

        async def fake_handle_client(reader, writer, service_args):
            dispatched["reader"] = reader
            dispatched["writer"] = writer
            dispatched["service"] = service_args.active_service

        monkeypatch.setattr(ro, "handle_client", fake_handle_client)
        await ro.run_services(root, admin_server_factory=lambda _args: admin)
        assert len(started) == 1
        callback, host, port = started[0]
        assert (host, port) == ("127.0.0.1", 19777)
        reader = object()
        writer = object()
        await callback(reader, writer)
        assert dispatched == {"reader": reader, "writer": writer, "service": "meganet"}
        assert server.closed and server.waited
        assert admin.shutdown_calls == 1
        assert admin.server_close_calls == 1

    asyncio.run(scenario())


def test_orchestration_shared_endpoint_uses_one_unified_listener(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path)
        root = argparse.Namespace()
        endpoints = {
            "meganet": _service_ns("meganet", "tcp", host="0.0.0.0", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "tcp", host="0.0.0.0", port=19777, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])
        calls: list[tuple[object, str, int]] = []
        server = _Server(19777)

        async def fake_start_server(callback, *, host, port):
            calls.append((callback, host, port))
            return server

        monkeypatch.setattr(ro.asyncio, "start_server", fake_start_server)
        dispatched = []

        async def fake_unified(reader, writer, args):
            dispatched.append((reader, writer, args))

        monkeypatch.setattr(ro, "handle_unified_tcp_client", fake_unified)
        await ro.run_services(root, admin_server_factory=lambda _args: None)
        assert len(calls) == 1
        callback, host, port = calls[0]
        assert (host, port) == ("0.0.0.0", 19777)
        reader = object()
        writer = object()
        await callback(reader, writer)
        assert dispatched == [(reader, writer, root)]

    asyncio.run(scenario())


def test_orchestration_serial_only_creates_serial_task(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path)
        root = argparse.Namespace()
        endpoints = {
            "meganet": _service_ns("meganet", "serial", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "off", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])
        seen = []

        async def fake_serial_loop(service_args):
            seen.append(service_args.active_service)

        monkeypatch.setattr(ro, "serial_server_loop", fake_serial_loop)
        monkeypatch.setattr(ro.asyncio, "start_server", lambda *_a, **_k: pytest.fail("TCP listener should not start"))
        await ro.run_services(root, admin_server_factory=lambda _args: None)
        assert seen == ["meganet"]

    asyncio.run(scenario())


def test_orchestration_adapter_start_failure_still_shuts_down_admin(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path, adapter_enabled=True)
        root = argparse.Namespace()
        admin = _AdminServer()
        endpoints = {
            "meganet": _service_ns("meganet", "tcp", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "off", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])

        async def fail_start_server(*_args, **_kwargs):
            raise OSError("bind failed")

        monkeypatch.setattr(ro.asyncio, "start_server", fail_start_server)
        with pytest.raises(OSError, match="bind failed"):
            await ro.run_services(root, admin_server_factory=lambda _args: admin)
        assert admin.shutdown_calls == 1
        assert admin.server_close_calls == 1

    asyncio.run(scenario())


def test_orchestration_waits_for_cancelled_sibling_cleanup(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path)
        root = argparse.Namespace()
        endpoints = {
            "meganet": _service_ns("meganet", "serial", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "tcp", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])
        serial_started = asyncio.Event()
        serial_cleaned = asyncio.Event()

        async def fake_serial_loop(_service_args):
            serial_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                serial_cleaned.set()
                raise

        monkeypatch.setattr(ro, "serial_server_loop", fake_serial_loop)
        server = _Server(19779, fail=RuntimeError("listener failed"))

        async def fake_start_server(*_args, **_kwargs):
            return server

        monkeypatch.setattr(ro.asyncio, "start_server", fake_start_server)
        with pytest.raises(RuntimeError, match="listener failed"):
            await ro.run_services(root, admin_server_factory=lambda _args: None)
        assert serial_started.is_set()
        assert serial_cleaned.is_set(), "run_services returned before cancelled sibling finished cleanup"

    asyncio.run(scenario())


def _config_namespace(**overrides) -> argparse.Namespace:
    values = dict(
        meganet_transport="tcp",
        meganet_host="127.0.0.1",
        meganet_port=19777,
        meganet_serial_port="COM12",
        meganet_serial_baud=115200,
        telebradesco_transport="tcp",
        telebradesco_host="127.0.0.1",
        telebradesco_port=19779,
        telebradesco_serial_port="COM10",
        telebradesco_serial_baud=115200,
        telebradesco_adapter_enabled=False,
        admin_web=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_endpoint_validation_allows_exact_shared_tcp_and_rejects_serial_collision():
    shared = _config_namespace(telebradesco_port=19777)
    rc.validate_dual_endpoints(shared)
    assert shared._service_arg_refs["meganet"].port == 19777
    assert shared._service_arg_refs["telebradesco"].port == 19777

    conflict = _config_namespace(
        meganet_transport="serial",
        telebradesco_transport="serial",
        meganet_serial_port="COM12",
        telebradesco_serial_port="com12",
    )
    with pytest.raises(ValueError, match="Conflito serial"):
        rc.validate_dual_endpoints(conflict)


def test_endpoint_validation_rejects_no_enabled_service():
    args = _config_namespace(meganet_transport="off", telebradesco_transport="off")
    with pytest.raises(ValueError, match="Nenhum serviço"):
        rc.validate_dual_endpoints(args)


def test_refresh_app_config_is_observed_by_new_service_args():
    args = _config_namespace()
    refresh_app_config(args)
    assert rc.service_args(args, "meganet").port == 19777
    args.meganet_port = 21000
    refresh_app_config(args)
    assert rc.service_args(args, "meganet").port == 21000


class _FakeSerial:
    def __init__(self, *, read_values=None, write_error=None, flush_error=None) -> None:
        self.is_open = True
        self.read_values = list(read_values or [])
        self.write_error = write_error
        self.flush_error = flush_error
        self.writes: list[bytes] = []
        self.flush_calls = 0
        self.close_calls = 0
        self.read_calls = 0

    def read(self, n: int) -> bytes:
        self.read_calls += 1
        if self.read_values:
            value = self.read_values.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        return b""

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_error is not None:
            raise self.flush_error

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False


def test_serial_reader_yields_when_idle_and_returns_data(monkeypatch):
    async def scenario() -> None:
        ser = _FakeSerial(read_values=[b"", b"", b"abc"])
        reader = rserial._SerialAsyncReader(ser)
        original_sleep = asyncio.sleep
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(rserial.asyncio, "sleep", fake_sleep)
        assert await reader.read(16) == b"abc"
        assert ser.read_calls == 3
        assert sleeps == [0.005, 0.005]

    asyncio.run(scenario())


def test_serial_reader_returns_eof_when_closed_or_read_fails():
    async def scenario() -> None:
        closed = _FakeSerial()
        closed.is_open = False
        assert await rserial._SerialAsyncReader(closed).read() == b""

        failed = _FakeSerial(read_values=[OSError("carrier lost")])
        assert await rserial._SerialAsyncReader(failed).read() == b""

    asyncio.run(scenario())


def test_serial_writer_open_write_flush_close_and_is_closing():
    async def scenario() -> None:
        ser = _FakeSerial()
        writer = rserial._SerialAsyncWriter(ser, "COM12", 115200)
        assert writer.get_extra_info("peername") == ("COM12", 115200)
        assert writer.get_extra_info("other", "fallback") == "fallback"
        assert writer.is_closing() is False
        writer.write(b"abc")
        await writer.drain()
        assert ser.writes == [b"abc"]
        assert ser.flush_calls == 1
        writer.close()
        await writer.wait_closed()
        assert ser.close_calls == 1
        assert writer.is_closing() is True

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_point", ["write", "flush"])
def test_serial_io_failure_triggers_reopen(monkeypatch, failure_point):
    async def scenario() -> None:
        opened: list[_FakeSerial] = []
        reopened = asyncio.Event()

        def serial_factory(**_kwargs):
            if not opened:
                ser = _FakeSerial(
                    write_error=OSError("write failed") if failure_point == "write" else None,
                    flush_error=OSError("flush failed") if failure_point == "flush" else None,
                )
            else:
                ser = _FakeSerial()
                reopened.set()
            opened.append(ser)
            return ser

        monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=serial_factory))
        original_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await original_sleep(0)

        monkeypatch.setattr(rserial.asyncio, "sleep", fast_sleep)

        async def fake_handle_client(_reader, writer, _args):
            writer.write(b"probe")
            await writer.drain()
            await asyncio.Future()

        monkeypatch.setattr(rserial, "handle_client", fake_handle_client)
        args = argparse.Namespace(active_service="meganet", serial_port="COM12", serial_baud=115200)
        task = asyncio.create_task(rserial.serial_server_loop(args))
        try:
            await asyncio.wait_for(reopened.wait(), timeout=0.2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(opened) >= 2
        assert opened[0].close_calls >= 1

    asyncio.run(scenario())


def test_serial_loop_cancellation_closes_open_port(monkeypatch):
    async def scenario() -> None:
        ser = _FakeSerial()
        monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=lambda **_kwargs: ser))
        entered = asyncio.Event()

        async def fake_handle_client(_reader, _writer, _args):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(rserial, "handle_client", fake_handle_client)
        args = argparse.Namespace(active_service="telebradesco", serial_port="COM10", serial_baud=115200)
        task = asyncio.create_task(rserial.serial_server_loop(args))
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert ser.close_calls == 1
        assert ser.is_open is False

    asyncio.run(scenario())


@pytest.mark.parametrize("service", ["meganet", "telebradesco"])
def test_serial_loop_dispatches_selected_service(monkeypatch, service):
    async def scenario() -> None:
        ser = _FakeSerial()
        monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=lambda **_kwargs: ser))
        seen = asyncio.Event()
        observed = {}

        async def fake_handle_client(reader, writer, args):
            observed["service"] = args.active_service
            observed["peer"] = writer.get_extra_info("peername")
            observed["reader_type"] = type(reader).__name__
            seen.set()
            await asyncio.Future()

        monkeypatch.setattr(rserial, "handle_client", fake_handle_client)
        args = argparse.Namespace(
            active_service=service,
            serial_port="COM12" if service == "meganet" else "COM10",
            serial_baud=115200,
        )
        task = asyncio.create_task(rserial.serial_server_loop(args))
        await asyncio.wait_for(seen.wait(), timeout=0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert observed["service"] == service
        assert observed["peer"] == (args.serial_port, 115200)
        assert observed["reader_type"] == "_SerialAsyncReader"

    asyncio.run(scenario())


class _RuntimeSpy:
    def __init__(self) -> None:
        self.events = []

    def register(self, writer, service, peer):
        self.events.append(("register", writer, service, peer))

    def unregister(self, writer, peer):
        self.events.append(("unregister", writer, peer))

    def record_traffic_for_writer(self, writer, service, direction, size):
        self.events.append(("traffic", writer, service, direction, size))

    def update_access_identity(self, service, peer, identity):
        self.events.append(("identity", service, peer, identity))


class _StreamWriter:
    def __init__(self, peer=("COM12", 115200)) -> None:
        self.peer = peer
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)

    def is_closing(self):
        return self.closed


class _ChunkReader:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    async def read(self, _n=4096):
        return self.chunks.pop(0) if self.chunks else b""


def test_runtime_transport_meganet_dispatch_keeps_traffic_accounting(monkeypatch):
    async def scenario() -> None:
        runtime = _RuntimeSpy()
        args = argparse.Namespace(active_service="meganet", _service_runtime=runtime)
        original_writer = _StreamWriter()
        reader = _ChunkReader(b"abc")
        observed = {}

        async def fake_meganet(r, w, passed_args):
            observed["data"] = await r.read(4096)
            w.write(b"reply")
            await w.drain()
            observed["args"] = passed_args

        monkeypatch.setattr(rt, "handle_meganet_client", fake_meganet)
        await rt.handle_client(reader, original_writer, args)
        assert observed["data"] == b"abc"
        assert bytes(original_writer.data) == b"reply"
        assert ("traffic", original_writer, "meganet", "rx", 3) in runtime.events
        assert ("traffic", original_writer, "meganet", "tx", 5) in runtime.events
        assert runtime.events[0][0] == "register"
        assert runtime.events[-1][0] == "unregister"

    asyncio.run(scenario())


def test_runtime_transport_telebradesco_serial_dispatch_and_cleanup(monkeypatch, tmp_path):
    async def scenario() -> None:
        runtime = _RuntimeSpy()
        args = argparse.Namespace(
            active_service="telebradesco",
            _service_runtime=runtime,
            telebradesco_accounts_file=tmp_path / "accounts.json",
            telebradesco_content_file=tmp_path / "accounts_content.json",
            telebradesco_block_mode="native-020",
            telebradesco_error_hold_seconds=2.0,
        )
        writer = _StreamWriter(peer=("COM10", 115200))
        reader = _ChunkReader(b"one", b"two", b"")
        instances = []

        class FakeTeleSession:
            def __init__(self, wrapped_writer, **kwargs):
                self.writer = wrapped_writer
                self.kwargs = kwargs
                self.close_task = None
                self.started = False
                self.fed = []
                instances.append(self)

            async def start(self):
                self.started = True
                self.kwargs["access_identity_callback"]("2924-0023456-7")

            async def feed(self, data):
                self.fed.append(data)
                self.writer.write(b"x")

        monkeypatch.setattr(rt, "TelebradescoSession", FakeTeleSession)
        await rt.handle_client(reader, writer, args)
        session = instances[0]
        assert session.started is True
        assert session.fed == [b"one", b"two"]
        assert writer.closed is True
        assert bytes(writer.data) == b"xx"
        assert ("traffic", writer, "telebradesco", "rx", 3) in runtime.events
        assert ("traffic", writer, "telebradesco", "tx", 1) in runtime.events
        assert ("identity", "telebradesco", "COM10:115200", "2924-0023456-7") in runtime.events
        assert runtime.events[-1][0] == "unregister"

    asyncio.run(scenario())


def test_orchestration_adapter_success_dispatches_internal_protocol(monkeypatch, tmp_path):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path, adapter_enabled=True)
        root = argparse.Namespace()
        endpoints = {
            "meganet": _service_ns("meganet", "tcp", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "off", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])
        calls = []

        async def fake_start_server(callback, *, host, port):
            calls.append((callback, host, port))
            return _Server(port)

        monkeypatch.setattr(ro.asyncio, "start_server", fake_start_server)
        dispatched = []

        async def fake_adapter(reader, writer, args):
            dispatched.append((reader, writer, args))

        monkeypatch.setattr(ro, "handle_telebradesco_adapter_client", fake_adapter)
        await ro.run_services(root, admin_server_factory=lambda _args: None)
        assert [(host, port) for _cb, host, port in calls] == [
            ("127.0.0.1", 19780),
            ("127.0.0.1", 19777),
        ]
        reader = object()
        writer = object()
        await calls[0][0](reader, writer)
        assert dispatched == [(reader, writer, root)]

    asyncio.run(scenario())


def test_orchestration_invalid_accounts_database_does_not_skip_listener(monkeypatch, tmp_path, caplog):
    async def scenario() -> None:
        _patch_orchestration_basics(monkeypatch, tmp_path)
        root = argparse.Namespace()
        endpoints = {
            "meganet": _service_ns("meganet", "serial", host="127.0.0.1", port=19777, serial_port="COM12"),
            "telebradesco": _service_ns("telebradesco", "off", host="127.0.0.1", port=19779, serial_port="COM10"),
        }
        monkeypatch.setattr(ro, "service_args", lambda _args, service: endpoints[service])

        def fail_accounts(*_args, **_kwargs):
            raise ro.TelebradescoAccountsError("invalid database")

        monkeypatch.setattr(ro, "read_telebradesco_accounts", fail_accounts)
        started = []

        async def fake_serial_loop(service_args):
            started.append(service_args.active_service)

        monkeypatch.setattr(ro, "serial_server_loop", fake_serial_loop)
        await ro.run_services(root, admin_server_factory=lambda _args: None)
        assert started == ["meganet"]

    asyncio.run(scenario())
    assert "TELE_ACCOUNTS_STARTUP_UNAVAILABLE" in caplog.text


def test_serial_open_failure_retries_and_reopens(monkeypatch):
    async def scenario() -> None:
        attempts = 0
        opened = asyncio.Event()
        ser = _FakeSerial()

        def serial_factory(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("COM busy")
            opened.set()
            return ser

        monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=serial_factory))
        original_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await original_sleep(0)

        monkeypatch.setattr(rserial.asyncio, "sleep", fast_sleep)

        async def fake_handle_client(_reader, _writer, _args):
            await asyncio.Future()

        monkeypatch.setattr(rserial, "handle_client", fake_handle_client)
        args = argparse.Namespace(active_service="meganet", serial_port="COM12", serial_baud=115200)
        task = asyncio.create_task(rserial.serial_server_loop(args))
        try:
            await asyncio.wait_for(opened.wait(), timeout=0.2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert attempts >= 2
        assert ser.close_calls == 1

    asyncio.run(scenario())
