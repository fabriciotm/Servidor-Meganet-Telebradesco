from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest


from meganet_server.meganet import accounts, mail, protocol
from meganet_server.meganet.arq import MegaNetArqMixin


def _registration(db: accounts.CadastroDatabase, username: str, *, session_id: str | None = None):
    return db.upsert_registration(
        session_id=session_id or f"s-{username}",
        peer="127.0.0.1:1",
        profile_name=username.upper(),
        bootstrap_login="meganet",
        bootstrap_password="guerta",
        fields={
            "nome": username.title(),
            "nome_meganet": username,
            "senha": "abc123",
            "codigo_acesso": f"CODE-{username}",
        },
        packet_count=1,
    )


def test_cadastro_upsert_tolerates_corrupt_total_sessions(tmp_path):
    path = tmp_path / "cadastros.json"
    db = accounts.CadastroDatabase(path)
    _registration(db, "alice")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["users"]["alice"]["total_sessions"] = "invalid"
    path.write_text(json.dumps(data), encoding="utf-8")

    db2 = accounts.CadastroDatabase(path)
    _registration(db2, "alice", session_id="second")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["users"]["alice"]["total_sessions"] == 1
    assert persisted["users"]["alice"]["history"][-1]["session_id"] == "second"


def test_cadastro_concurrent_registrations_preserve_all_users(tmp_path):
    path = tmp_path / "cadastros.json"
    accounts.CadastroDatabase(path)._atomic_write()

    def worker(i: int) -> None:
        db = accounts.CadastroDatabase(path)
        _registration(db, f"user{i:02d}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(worker, i) for i in range(24)]
        for future in futures:
            future.result()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["users"]) == {f"user{i:02d}" for i in range(24)}


def test_cadastro_concurrent_access_updates_do_not_lose_counts(tmp_path):
    path = tmp_path / "cadastros.json"
    seed = accounts.CadastroDatabase(path)
    _registration(seed, "alice")

    def worker(i: int) -> None:
        db = accounts.CadastroDatabase(path)
        result = db.record_user_access(nome_meganet="alice", session_id=f"login-{i}", peer="peer")
        assert result is not None

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, i) for i in range(20)]
        for future in futures:
            future.result()

    data = json.loads(path.read_text(encoding="utf-8"))
    alice = data["users"]["alice"]
    assert alice["total_sessions"] == 21
    login_ids = {row["session_id"] for row in alice["history"] if str(row.get("session_id", "")).startswith("login-")}
    assert login_ids == {f"login-{i}" for i in range(20)}


def test_mail_concurrent_appends_preserve_all_messages(tmp_path):
    base = tmp_path / "mail"
    mail.MailStore(base)._ensure_mailbox("bob")

    def worker(i: int) -> None:
        store = mail.MailStore(base)
        store.append_to_mailbox("bob", {"id": f"m{i}", "subject": f"S{i}"})

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(worker, i) for i in range(30)]
        for future in futures:
            future.result()

    messages = mail.MailStore(base).load_mailbox_messages("bob")
    assert {m["id"] for m in messages} == {f"m{i}" for i in range(30)}
    assert len(messages) == 30


def test_password_change_store_concurrent_appends_preserve_all_events(tmp_path):
    base = tmp_path / "passwords"
    accounts.PasswordChangeStore(base)

    def worker(i: int) -> None:
        store = accounts.PasswordChangeStore(base)
        store.append({"session_id": f"s{i}", "kind": "probe", "timestamp": str(i)})

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, i) for i in range(20)]
        for future in futures:
            future.result()

    payload = json.loads((base / "password_change_probe.json").read_text(encoding="utf-8"))
    assert {e["session_id"] for e in payload["events"]} == {f"s{i}" for i in range(20)}


class _Writer:
    def __init__(self, *, closing=False, is_closing_error=False):
        self.closing = closing
        self.is_closing_error = is_closing_error
        self.closed = False

    def is_closing(self):
        if self.is_closing_error:
            raise RuntimeError("probe")
        return self.closing

    def close(self):
        self.closed = True


class _ArqHarness(MegaNetArqMixin):
    def __init__(self):
        self.writer = _Writer()
        self.packet_ack_waiting_seq = 7
        self.packet_wire_buffer = bytearray()
        self.packet_ack_queue = asyncio.Queue()
        self.peer = "test"
        self.packet_reader_closed = False
        self.packet_reader_close_reason = ""


def test_arq_routes_mixed_raw_control_and_client_frame_without_losing_bytes():
    async def run():
        h = _ArqHarness()
        q = asyncio.Queue()
        payload = b"A" + bytes([protocol.ACK]) + b"B"
        checksum = (len(payload) + 3 + sum(payload)) & 0xFFFF
        frame = bytes([protocol.STX, len(payload), 3]) + payload + bytes([checksum & 0xFF, checksum >> 8, protocol.ETX])
        h.route_inbound_wire_data(b"login:\r" + bytes([protocol.ACK]) + frame, q)
        assert await q.get() == b"login:\r"
        control, _when = h.packet_ack_queue.get_nowait()
        assert control == protocol.ACK
        assert await q.get() == frame
        assert h.packet_wire_buffer == bytearray()
    asyncio.run(run())


def test_arq_flushes_large_unframed_prefix_and_reader_close_is_idempotent():
    async def run():
        h = _ArqHarness()
        q = asyncio.Queue()
        h.route_inbound_wire_data(b"X" * 300, q)
        assert await q.get() == b"X" * 236
        assert bytes(h.packet_wire_buffer) == b"X" * 64
        h.flush_packet_wire_buffer(q)
        assert await q.get() == b"X" * 64
        h.notify_packet_reader_closed("eof")
        h.notify_packet_reader_closed("ignored")
        assert h.packet_reader_closed is True
        assert h.packet_reader_close_reason == "eof"
        assert h.packet_ack_queue.get_nowait() is None
        assert h.packet_ack_queue.empty()
    asyncio.run(run())


def test_arq_writer_is_closing_probe_failure_is_tolerated():
    h = _ArqHarness()
    h.writer = _Writer(is_closing_error=True)
    assert h._writer_is_closing() is False


def test_mail_archive_rolls_back_when_mailbox_clear_fails(tmp_path, monkeypatch):
    store = mail.MailStore(tmp_path / "mail")
    store.append_to_mailbox("bob", {"id": "m1", "subject": "TESTE", "body": "OLA"})
    mailbox_path = store.mailbox_path("bob")
    archive_path = store.base_dir / "downloaded_mail.json"
    original_write = store._write_list_file
    failed = False

    def fail_mailbox_clear(path, data):
        nonlocal failed
        if path == mailbox_path and data.get("messages") == [] and not failed:
            failed = True
            raise OSError("simulated mailbox clear failure")
        return original_write(path, data)

    monkeypatch.setattr(store, "_write_list_file", fail_mailbox_clear)
    with pytest.raises(OSError, match="mailbox clear failure"):
        store.clear_mailbox_messages("bob", archive=True, reason="rom-download")

    assert len(store.load_mailbox_messages("bob")) == 1
    if archive_path.exists():
        after_failure = json.loads(archive_path.read_text(encoding="utf-8"))
        assert after_failure.get("messages", []) == []

    monkeypatch.setattr(store, "_write_list_file", original_write)
    count, _ = store.clear_mailbox_messages("bob", archive=True, reason="rom-download")
    assert count == 1
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    assert [m["id"] for m in archive["messages"]] == ["m1"]
