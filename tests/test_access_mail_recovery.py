from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from meganet_server.meganet import mail
from meganet_server.runtime import statistics as rs


class _Writer:
    pass


def _use_log_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    access = tmp_path / "access"
    traffic = tmp_path / "traffic"
    access.mkdir()
    traffic.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic)
    if hasattr(rs, "_RUNTIME_PENDING_ACCESS"):
        rs._RUNTIME_PENDING_ACCESS.clear()
    if hasattr(rs, "_RUNTIME_PENDING_TRAFFIC"):
        rs._RUNTIME_PENDING_TRAFFIC.clear()
    return access, traffic


def _set_clock(monkeypatch, current: list[dt.datetime]) -> None:
    monkeypatch.setattr(rs, "_runtime_now_local", lambda: current[0])
    monkeypatch.setattr(
        rs.UnifiedServiceRuntime,
        "_minute_key",
        staticmethod(lambda now=None: (now or current[0]).strftime("%Y-%m-%d %H:%M")),
    )


def test_access_recovery_preserves_start_identity_and_end_across_midnight(tmp_path, monkeypatch) -> None:
    access, _traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 23, 59, 58)]
    _set_clock(monkeypatch, current)

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    writer = _Writer()
    real_atomic_write = rs.atomic_write_json
    fail_access = {"enabled": True}

    def transient_failure(path, payload, **kwargs):
        if fail_access["enabled"] and "access_20261004.json" in str(path):
            raise OSError("access filesystem unavailable")
        return real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(rs, "atomic_write_json", transient_failure)
    runtime.register(writer, "meganet", "10.0.0.1:1000")
    runtime.update_access_identity("meganet", "10.0.0.1:1000", "KAVERA")

    fail_access["enabled"] = False
    current[0] = dt.datetime(2026, 10, 5, 0, 0, 3)
    runtime.unregister(writer, "10.0.0.1:1000")

    day1 = json.loads((access / "access_20261004.json").read_text(encoding="utf-8"))
    day2 = json.loads((access / "access_20261005.json").read_text(encoding="utf-8"))

    assert len(day1["connections"]) == 1
    connection = day1["connections"][0]
    assert connection["identity"] == "KAVERA"
    assert connection["status"] == "encerrada"
    assert connection["end_at"] == "2026-10-05T00:00:03"

    assert [event["event"] for event in day1["events"]] == ["Conexão iniciada"]
    assert day1["events"][0]["identity"] == "KAVERA"
    assert [event["event"] for event in day2["events"]] == ["Conexão encerrada"]
    assert day2["events"][0]["identity"] == "KAVERA"

    runtime.loop.close()



def test_access_retry_after_partial_register_write_does_not_duplicate_connection(tmp_path, monkeypatch) -> None:
    access, _traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 12, 0, 0)]
    _set_clock(monkeypatch, current)

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    writer = _Writer()
    real_atomic_write = rs.atomic_write_json
    access_writes = {"count": 0}

    def fail_only_start_event_write(path, payload, **kwargs):
        if "access_20261004.json" in str(path):
            access_writes["count"] += 1
            if access_writes["count"] == 2:
                raise OSError("event write unavailable")
        return real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(rs, "atomic_write_json", fail_only_start_event_write)
    runtime.register(writer, "meganet", "10.0.0.2:1000")
    runtime.record_traffic_for_writer(writer, "meganet", "rx", 7)

    payload = json.loads((access / "access_20261004.json").read_text(encoding="utf-8"))
    assert len(payload["connections"]) == 1
    assert payload["connections"][0]["rx_bytes"] == 7
    assert [event["event"] for event in payload["events"]] == ["Conexão iniciada"]

    runtime.loop.close()

def _mail_messages(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("messages", [])


def test_internal_mail_rolls_back_outbox_when_mailbox_write_fails(tmp_path, monkeypatch) -> None:
    store = mail.MailStore(tmp_path / "mail")
    mailbox = store._ensure_mailbox("bob")
    original_write = store._write_list_file
    failed = {"done": False}

    def fail_mailbox(path, data):
        if path == mailbox and data.get("messages") and not failed["done"]:
            failed["done"] = True
            raise OSError("mailbox unavailable")
        return original_write(path, data)

    monkeypatch.setattr(store, "_write_list_file", fail_mailbox)
    entry = {"user": "alice", "from": "alice", "to": "bob", "subject": "TESTE", "body": "OLA"}
    with pytest.raises(OSError, match="mailbox unavailable"):
        store.append_outgoing(dict(entry))

    assert _mail_messages(store.outbox_email_path) == []
    assert _mail_messages(mailbox) == []

    monkeypatch.setattr(store, "_write_list_file", original_write)
    store.append_outgoing(dict(entry))
    assert len(_mail_messages(store.outbox_email_path)) == 1
    assert len(_mail_messages(mailbox)) == 1


def test_internal_mail_rolls_back_mailbox_when_final_outbox_write_fails(tmp_path, monkeypatch) -> None:
    store = mail.MailStore(tmp_path / "mail")
    mailbox = store._ensure_mailbox("bob")
    original_write = store._write_list_file
    outbox_writes = {"count": 0}

    def fail_final_outbox(path, data):
        if path == store.outbox_email_path:
            outbox_writes["count"] += 1
            if outbox_writes["count"] == 2:
                raise OSError("outbox finalization failed")
        return original_write(path, data)

    monkeypatch.setattr(store, "_write_list_file", fail_final_outbox)
    entry = {"user": "alice", "from": "alice", "to": "bob", "subject": "TESTE", "body": "OLA"}
    with pytest.raises(OSError, match="outbox finalization failed"):
        store.append_outgoing(dict(entry))

    assert _mail_messages(store.outbox_email_path) == []
    assert _mail_messages(mailbox) == []

    monkeypatch.setattr(store, "_write_list_file", original_write)
    store.append_outgoing(dict(entry))
    assert len(_mail_messages(store.outbox_email_path)) == 1
    assert len(_mail_messages(mailbox)) == 1
