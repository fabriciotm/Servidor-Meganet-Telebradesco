from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from meganet_server import server as facade
from meganet_server.runtime import configuration as rc
from meganet_server.runtime import statistics as rs
from meganet_server.runtime import unified as ru


class DummyWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_runtime_facade_reexports_match_modules():
    assert facade.UnifiedServiceRuntime is rs.UnifiedServiceRuntime
    assert facade._runtime_query_statistics is rs._runtime_query_statistics
    assert facade._unified_tcp_detect_service is ru._unified_tcp_detect_service
    assert facade._service_args is rc.service_args


def test_unified_detection_vectors_from_runtime_module():
    vectors = {
        b"TBA1": "telebradesco-adapter",
        b"MGA1": "meganet-auto-probe",
        b"AT&FV0B0+FCLASS=1\r": "telebradesco",
        b"ATX3DT,,777777\r": "telebradesco",
        b"ATX3DP,123456789\r": "meganet",
        b"#id kavera 123ABC SSO#\r": "meganet",
    }
    for raw, expected in vectors.items():
        assert ru._unified_tcp_detect_service(raw) == expected
    assert ru._unified_tcp_is_ambiguous_hayes_prefix(b"A")
    assert ru._unified_tcp_is_ambiguous_hayes_prefix(b"AT")
    assert ru._unified_tcp_is_meganet_empty_cr_signal(b"\r")
    assert ru._unified_tcp_find_corrupt_preamble(b"\x80") == (0, 0x80)


def test_service_args_preserves_runtime_reference():
    ns = argparse.Namespace(
        meganet_transport="tcp", meganet_host="0.0.0.0", meganet_port=19777,
        meganet_serial_port="COM12", meganet_serial_baud=115200,
        telebradesco_transport="tcp", telebradesco_host="127.0.0.1", telebradesco_port=19779,
        telebradesco_serial_port="COM10", telebradesco_serial_baud=115200,
        _service_runtime=object(),
    )
    mega = rc.service_args(ns, "meganet")
    assert mega.active_service == "meganet"
    assert mega.host == "0.0.0.0"
    assert mega.port == 19777
    assert mega._service_runtime is ns._service_runtime
    assert ns._service_arg_refs["meganet"] is mega


def test_runtime_persistent_access_and_filters(tmp_path, monkeypatch):
    access = tmp_path / "access"
    traffic = tmp_path / "traffic"
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic)

    async def scenario() -> None:
        runtime = rs.UnifiedServiceRuntime(asyncio.get_running_loop())
        writer = DummyWriter()
        runtime.register(writer, "meganet", "127.0.0.1:1234")
        runtime.update_access_identity("meganet", "127.0.0.1:1234", "kavera")
        runtime.record_traffic_for_writer(writer, "meganet", "rx", 10)
        runtime.record_traffic_for_writer(writer, "meganet", "tx", 20)
        runtime.unregister(writer, "127.0.0.1:1234")

    asyncio.run(scenario())

    today = dt.date.today().isoformat()
    stats = rs._runtime_query_statistics({
        "date_from": [today], "date_to": [today],
        "service": ["meganet"], "user": ["kavera"],
    })
    assert len(stats["connections"]) == 1
    conn = stats["connections"][0]
    assert conn["service"] == "meganet"
    assert conn["identity"] == "kavera"
    assert conn["rx_bytes"] == 10
    assert conn["tx_bytes"] == 20
    assert stats["totals"]["meganet"] == {"tx": 20, "rx": 10}

    no_match = rs._runtime_query_statistics({
        "date_from": [today], "date_to": [today],
        "service": ["telebradesco"], "user": ["kavera"],
    })
    assert no_match["connections"] == []

class UnifiedWriter:
    def __init__(self) -> None:
        self.closed = False
        self.data = bytearray()

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("127.0.0.1", 45678)
        return default

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed


def _stream_reader(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    if data:
        reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def test_unified_mga1_mgr1_reservation_dispatches_meganet_and_preserves_tail(monkeypatch):
    async def scenario() -> None:
        dispatched = {}

        def fake_service_args(_args, service):
            return argparse.Namespace(active_service=service)

        async def fake_handle_client(reader, writer, service_args):
            dispatched["service"] = service_args.active_service
            dispatched["online_start"] = service_args.online_start
            dispatched["ready_seen"] = service_args.meganet_auto_ready_seen
            dispatched["payload"] = await reader.read(4096)

        monkeypatch.setattr(ru, "_service_args", fake_service_args)
        monkeypatch.setattr(ru, "handle_client", fake_handle_client)
        args = argparse.Namespace(
            unified_tcp_detect_timeout=0.2,
            unified_tcp_default_service="meganet",
            meganet_auto_ready_timeout=0.5,
        )
        writer = UnifiedWriter()
        await ru.handle_unified_tcp_client(_stream_reader(b"MGA1MGR1TAIL"), writer, args)
        assert dispatched == {
            "service": "meganet",
            "online_start": True,
            "ready_seen": True,
            "payload": b"TAIL",
        }
        assert writer.closed is False

    asyncio.run(scenario())


def test_unified_mga1_without_mgr1_closes_without_dispatch(monkeypatch):
    async def scenario() -> None:
        dispatched = False

        async def fake_handle_client(*_args, **_kwargs):
            nonlocal dispatched
            dispatched = True

        monkeypatch.setattr(ru, "handle_client", fake_handle_client)
        args = argparse.Namespace(
            unified_tcp_detect_timeout=0.2,
            unified_tcp_default_service="meganet",
            meganet_auto_ready_timeout=0.5,
        )
        writer = UnifiedWriter()
        await ru.handle_unified_tcp_client(_stream_reader(b"MGA1"), writer, args)
        assert dispatched is False
        assert writer.closed is True

    asyncio.run(scenario())


def test_unified_ambiguous_hayes_prefix_prefers_silent_telebradesco(monkeypatch):
    async def scenario() -> None:
        dispatched = {}

        def fake_service_args(_args, service):
            return argparse.Namespace(active_service=service)

        async def fake_handle_client(reader, _writer, service_args):
            dispatched["service"] = service_args.active_service
            dispatched["payload"] = await reader.read(4096)

        monkeypatch.setattr(ru, "_service_args", fake_service_args)
        monkeypatch.setattr(ru, "handle_client", fake_handle_client)
        args = argparse.Namespace(
            unified_tcp_detect_timeout=0.2,
            unified_tcp_default_service="meganet",
        )
        writer = UnifiedWriter()
        await ru.handle_unified_tcp_client(_stream_reader(b"A"), writer, args)
        assert dispatched == {"service": "telebradesco", "payload": b"A"}

    asyncio.run(scenario())


def test_unified_corrupt_high_bit_preamble_closes_without_dispatch(monkeypatch):
    async def scenario() -> None:
        dispatched = False

        async def fake_handle_client(*_args, **_kwargs):
            nonlocal dispatched
            dispatched = True

        monkeypatch.setattr(ru, "handle_client", fake_handle_client)
        args = argparse.Namespace(
            unified_tcp_detect_timeout=0.2,
            unified_tcp_default_service="meganet",
        )
        writer = UnifiedWriter()
        await ru.handle_unified_tcp_client(_stream_reader(b"\x80"), writer, args)
        assert dispatched is False
        assert writer.closed is True

    asyncio.run(scenario())
