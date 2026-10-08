from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from meganet_server import api
from meganet_server.meganet import content_store
from meganet_server.meganet import transport as mt
from meganet_server.runtime import statistics as rs
from meganet_server.runtime import transport as rt


class _EOFReader:
    async def read(self, _n: int = 4096) -> bytes:
        return b""


class _Writer:
    def __init__(self, peer=("127.0.0.1", 12345)) -> None:
        self.peer = peer
        self.closed = False

    def get_extra_info(self, name: str, default=None):
        return self.peer if name == "peername" else default

    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed


def _use_runtime_dirs(monkeypatch, tmp_path: Path) -> None:
    access = tmp_path / "access"
    traffic = tmp_path / "traffic"
    access.mkdir()
    traffic.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic)


def test_first_traffic_delta_after_midnight_is_not_lost_from_dashboard(tmp_path, monkeypatch) -> None:
    _use_runtime_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 23, 59, 59)]
    monkeypatch.setattr(rs, "_runtime_now_local", lambda: current[0])
    monkeypatch.setattr(
        rs.UnifiedServiceRuntime,
        "_minute_key",
        staticmethod(lambda now=None: (now or current[0]).strftime("%Y-%m-%d %H:%M")),
    )

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    current[0] = dt.datetime(2026, 10, 5, 0, 0, 1)
    runtime.record_traffic("meganet", "tx", 25)

    snapshot = runtime.dashboard_snapshot()
    assert snapshot["traffic"]["meganet"]["tx"] == 25
    assert snapshot["buckets"][-1]["meganet_tx"] == 25


def test_meganet_transport_awaits_all_session_background_task_cleanup(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        cleanup_finished = asyncio.Event()
        instances = []

        async def background() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleanup_finished.set()
                raise

        class FakeSession:
            def __init__(self, writer, **kwargs):
                self.writer = writer
                self.peer = kwargs.get("peer", "unknown")
                self.bootstrap_terminate_requested = False
                self.packet_delivery_failed = False
                self.profile_idle_task = None
                self.post_dle_task = None
                self.close_task = None
                self.bootstrap_watchdog_task = None
                self.login_prompt_retry_task = None
                self.meganet_packet_ack_mode = "off"
                self.packet_tx_attempts = 0
                self.packet_tx_confirmed = 0
                self.packet_tx_retransmissions = 0
                self.packet_tx_naks = 0
                self.packet_tx_timeouts = 0
                self.packet_tx_failures = 0
                instances.append(self)

            async def start(self):
                self.login_prompt_retry_task = asyncio.create_task(background())
                await asyncio.sleep(0)

            def route_inbound_wire_data(self, data, queue):
                queue.put_nowait(data)

            def flush_packet_wire_buffer(self, queue):
                pass

            def notify_packet_reader_closed(self, reason):
                pass

            def cancel_bootstrap_watchdog(self):
                if self.bootstrap_watchdog_task and not self.bootstrap_watchdog_task.done():
                    self.bootstrap_watchdog_task.cancel()
                self.bootstrap_watchdog_task = None

        monkeypatch.setattr(mt, "_prepare_meganet_session_args", lambda args: args)
        monkeypatch.setattr(mt, "MegaNetSession", FakeSession)

        args = api.parse_args(["--config", str(tmp_path / "cfg.json")])
        writer = _Writer()
        await mt.handle_meganet_client(_EOFReader(), writer, args)

        task = instances[0].login_prompt_retry_task
        assert task is not None
        assert task.done()
        assert task.cancelled()
        assert cleanup_finished.is_set()

    asyncio.run(scenario())


def test_telebradesco_transport_awaits_delayed_close_task_cleanup(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        cleanup_finished = asyncio.Event()
        instances = []

        async def delayed_close() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleanup_finished.set()
                raise

        class FakeTeleSession:
            def __init__(self, writer, **kwargs):
                self.writer = writer
                self.close_task = None
                instances.append(self)

            async def start(self):
                self.close_task = asyncio.create_task(delayed_close())
                await asyncio.sleep(0)

            async def feed(self, data):
                pass

        monkeypatch.setattr(rt, "TelebradescoSession", FakeTeleSession)
        args = argparse.Namespace(
            active_service="telebradesco",
            _service_runtime=None,
            telebradesco_accounts_file=tmp_path / "accounts.json",
            telebradesco_content_file=tmp_path / "accounts_content.json",
            telebradesco_block_mode="native-020",
            telebradesco_error_hold_seconds=2.0,
        )
        writer = _Writer()
        await rt.handle_client(_EOFReader(), writer, args)

        task = instances[0].close_task
        assert task is not None
        assert task.done()
        assert task.cancelled()
        assert cleanup_finished.is_set()

    asyncio.run(scenario())


def test_corrupt_existing_meganet_content_is_not_overwritten_by_runtime_recovery(tmp_path) -> None:
    target = tmp_path / "content.json"
    original = b'{"message_lines": ["RECUPERAR"],'
    target.write_bytes(original)

    recovered = content_store.read_content_file(target, create_if_missing=True)

    assert recovered["schema"] == "meganet_content_v1"
    assert target.read_bytes() == original


def test_password_change_csv_failure_does_not_invalidate_primary_json(tmp_path, monkeypatch) -> None:
    from meganet_server.meganet.accounts import PasswordChangeStore

    store = PasswordChangeStore(tmp_path / "password-change")

    def fail_csv(_events):
        raise OSError("derived csv unavailable")

    monkeypatch.setattr(store, "_write_csv", fail_csv)
    entry = {"timestamp": "2026-10-04T23:30:00", "kind": "token", "user": "KAVERA"}

    path = store.append(entry)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["events"] == [entry]
