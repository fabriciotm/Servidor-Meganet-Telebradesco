from __future__ import annotations

import argparse
import asyncio
import json
import struct

from meganet_server.meganet import accounts as m_accounts
from meganet_server.meganet.mail import MailStore
from meganet_server.telebradesco import accounts as t_accounts
from meganet_server.telebradesco import adapter, content as t_content


class CaptureWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.drain_count = 0

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 43210)
        return default

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.drain_count += 1
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def _seed_tele_account(tmp_path, *, password: str = "1234", blocked: bool = False):
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key = t_accounts.telebradesco_account_key("2924", "0023456", "7")
    store = t_accounts.default_telebradesco_accounts()
    store["accounts"][key] = {
        "nome": "CONTA TESTE",
        "agencia": "2924",
        "conta": "0023456",
        "digito": "7",
        "senha": password,
        "bloqueada": blocked,
        "content": t_content.default_telebradesco_content(),
    }
    t_accounts.write_telebradesco_accounts(
        accounts_path,
        store,
        accounts_content_file=content_path,
    )
    return accounts_path, content_path, key


def _adapter_request(login: bytes, *, magic: bytes = b"TBA1", version: int = 1, flags: int = 0) -> bytes:
    return adapter.TELEBRADESCO_ADAPTER_REQUEST_HEADER.pack(magic, version, flags, len(login)) + login


def _adapter_args(tmp_path, accounts_path, content_path):
    return argparse.Namespace(
        telebradesco_adapter_timeout=0.25,
        telebradesco_adapter_log_file=tmp_path / "adapter.jsonl",
        telebradesco_accounts_file=accounts_path,
        telebradesco_content_file=content_path,
        telebradesco_block_mode="native-020",
    )


def _parse_adapter_response(raw: bytes) -> dict:
    h = adapter.TELEBRADESCO_ADAPTER_RESPONSE_HEADER
    magic, version, result, frame_count, stream_size, account_len, status_len = h.unpack(raw[:h.size])
    pos = h.size
    account = raw[pos:pos + account_len].decode("ascii")
    pos += account_len
    status = raw[pos:pos + status_len].decode("ascii")
    pos += status_len
    frames: list[bytes] = []
    for _ in range(frame_count):
        frame_len = struct.unpack("!H", raw[pos:pos + 2])[0]
        pos += 2
        frames.append(raw[pos:pos + frame_len])
        pos += frame_len
    assert pos == len(raw)
    return {
        "magic": magic,
        "version": version,
        "result": result,
        "frame_count": frame_count,
        "stream_size": stream_size,
        "account": account,
        "status": status,
        "frames": frames,
    }


async def _run_adapter(request: bytes, args) -> CaptureWriter:
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()
    writer = CaptureWriter()
    await adapter.handle_telebradesco_adapter_client(reader, writer, args)
    assert writer.closed is True
    return writer


def test_adapter_success_returns_frames_and_records_masked_access(tmp_path):
    accounts_path, content_path, key = _seed_tele_account(tmp_path)
    login = b"MEGABRA292400234567123412345678"
    args = _adapter_args(tmp_path, accounts_path, content_path)
    writer = asyncio.run(_run_adapter(_adapter_request(login), args))
    response = _parse_adapter_response(bytes(writer.data))

    assert response["magic"] == b"TBR1"
    assert response["version"] == 1
    assert response["result"] == 0
    assert response["status"] == "ok"
    assert response["account"] == key
    assert response["frame_count"] == 4
    assert [len(frame) for frame in response["frames"]] == [121, 121, 121, 84]
    assert response["stream_size"] == sum(len(frame) - 1 for frame in response["frames"])
    assert all(frame[-1] == 0x20 for frame in response["frames"])

    saved = json.loads(accounts_path.read_text(encoding="utf-8"))
    account = saved["accounts"][key]
    assert account["total_accesses"] == 1
    history_login = account["access_history"][-1]["login_text"]
    assert history_login == "MEGABRA292400234567****12345678"

    log_text = (tmp_path / "adapter.jsonl").read_text(encoding="utf-8")
    assert "MEGABRA292400234567123412345678" not in log_text
    assert "MEGABRA292400234567****12345678" in log_text


def test_adapter_invalid_password_returns_status_frames_not_transport_error(tmp_path):
    accounts_path, content_path, key = _seed_tele_account(tmp_path)
    login = b"MEGABRA292400234567999912345678"
    args = _adapter_args(tmp_path, accounts_path, content_path)
    writer = asyncio.run(_run_adapter(_adapter_request(login), args))
    response = _parse_adapter_response(bytes(writer.data))

    assert response["result"] == 0
    assert response["status"] == "invalid_password"
    assert response["account"] == key
    assert response["frame_count"] >= 1
    saved = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert int(saved["accounts"][key].get("total_accesses") or 0) == 0


def test_adapter_rejects_flags_length_non_ascii_and_missing_accounts(tmp_path):
    accounts_path, content_path, _key = _seed_tele_account(tmp_path)
    args = _adapter_args(tmp_path, accounts_path, content_path)

    async def one(request: bytes):
        writer = await _run_adapter(request, args)
        return _parse_adapter_response(bytes(writer.data))

    bad_flags = asyncio.run(one(_adapter_request(b"X", flags=1)))
    assert (bad_flags["result"], bad_flags["status"]) == (1, "bad_request")

    non_ascii = asyncio.run(one(_adapter_request(b"\xff")))
    assert (non_ascii["result"], non_ascii["status"]) == (1, "non_ascii_login")

    broken_accounts = tmp_path / "broken.json"
    broken_accounts.write_text("{not-json", encoding="utf-8")
    missing_args = _adapter_args(tmp_path, broken_accounts, tmp_path / "broken-content.json")
    unavailable = asyncio.run(_run_adapter(_adapter_request(b"MEGABRA292400234567123412345678"), missing_args))
    parsed = _parse_adapter_response(bytes(unavailable.data))
    assert (parsed["result"], parsed["status"]) == (2, "accounts_unavailable")


def test_adapter_truncated_request_logs_without_fabricating_response(tmp_path):
    accounts_path, content_path, _key = _seed_tele_account(tmp_path)
    args = _adapter_args(tmp_path, accounts_path, content_path)
    # Header declares 20 login bytes but EOF arrives after three.
    request = adapter.TELEBRADESCO_ADAPTER_REQUEST_HEADER.pack(b"TBA1", 1, 0, 20) + b"ABC"
    writer = asyncio.run(_run_adapter(request, args))
    assert bytes(writer.data) == b""
    log_lines = (tmp_path / "adapter.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"reason": "truncated"' in line for line in log_lines)


def test_adapter_internal_compilation_failure_returns_result_3(tmp_path, monkeypatch):
    accounts_path, content_path, _key = _seed_tele_account(tmp_path)
    args = _adapter_args(tmp_path, accounts_path, content_path)

    def invalid_compiled(*_args, **_kwargs):
        return {"frames": [], "stream_size": 0}

    monkeypatch.setattr(adapter, "compile_telebradesco_content", invalid_compiled)
    request = _adapter_request(b"MEGABRA292400234567123412345678")
    writer = asyncio.run(_run_adapter(request, args))
    response = _parse_adapter_response(bytes(writer.data))
    assert (response["result"], response["status"]) == (3, "internal_error")


def _seed_meganet_users(path):
    db = m_accounts.CadastroDatabase(path)
    for username in ("alice", "bob"):
        db.upsert_registration(
            session_id=f"seed-{username}",
            peer="127.0.0.1:1",
            profile_name=username.upper(),
            bootstrap_login="meganet",
            bootstrap_password="guerta",
            fields={
                "nome": username.title(),
                "nome_meganet": username,
                "senha": "abc123",
                "codigo_acesso": "ABCDEFGHIJB7",
                "email_meganet": f"{username}@meganet.tectoy.com.br",
            },
            packet_count=1,
        )
    return db


def test_mail_internal_delivery_download_archive_and_one_shot_notice(tmp_path):
    db = _seed_meganet_users(tmp_path / "cadastros.json")
    store = MailStore(tmp_path / "mail")
    entry = {
        "user": "alice",
        "from": "alice@meganet.tectoy.com.br",
        "to": "bob@meganet.tectoy.com.br",
        "subject": "TESTE",
        "body": "OLA BOB",
    }
    store.append_outgoing(entry, db)

    assert entry["delivery_status"] == "delivered-internal"
    assert entry["delivered_to"] == "bob@meganet.tectoy.com.br"
    inbox = store.load_mailbox_messages("bob")
    assert len(inbox) == 1
    assert inbox[0]["subject"] == "TESTE"
    assert store.mailbox_panel_notice_count("bob") == 1

    count, mailbox_path = store.clear_mailbox_messages("bob", archive=True, reason="rom-download")
    assert count == 1
    assert store.load_mailbox_messages("bob") == []
    assert store.mailbox_panel_notice_count("bob") == 1
    archive = json.loads((tmp_path / "mail" / "downloaded_mail.json").read_text(encoding="utf-8"))
    assert archive["messages"][-1]["download_reason"] == "rom-download"
    assert archive["messages"][-1]["downloaded_mailbox"] == "bob@meganet.tectoy.com.br"

    cleared, _path = store.clear_mailbox_panel_notice("bob")
    assert cleared == 1
    assert store.mailbox_panel_notice_count("bob") == 0
    persisted = json.loads(mailbox_path.read_text(encoding="utf-8"))
    assert persisted["panel_notice_clear_reason"] == "connection_panel_shown"


def test_mail_delivery_routes_admin_suggestions_external_unknown_and_existing_mailbox(tmp_path):
    db = _seed_meganet_users(tmp_path / "cadastros.json")
    store = MailStore(tmp_path / "mail")

    admin = {"from": "alice", "to": "admin", "subject": "A", "body": "1"}
    store.append_outgoing(admin, db)
    assert admin["delivery_status"] == "delivered-admin"

    suggestion = {"from": "alice", "to": "sugestoes", "subject": "S", "body": "2"}
    store.append_outgoing(suggestion, db)
    assert suggestion["delivery_status"] == "delivered-sugestao"

    external = {"from": "alice", "to": "pessoa@example.net", "subject": "E", "body": "3"}
    store.append_outgoing(external, db)
    assert external["delivery_status"] == "external-queued"

    unknown = {"from": "alice", "to": "ghost@meganet.tectoy.com.br", "subject": "U", "body": "4"}
    store.append_outgoing(unknown, db)
    assert unknown["delivery_status"] == "unknown-internal-recipient"
    assert not store.mailbox_path("ghost").exists()

    store._ensure_mailbox("legacy@meganet.tectoy.com.br")
    legacy = {"from": "alice", "to": "legacy@meganet.tectoy.com.br", "subject": "L", "body": "5"}
    store.append_outgoing(legacy, db)
    assert legacy["delivery_status"] == "delivered-internal-mailbox"
    assert store.load_mailbox_messages("legacy")[0]["subject"] == "L"

    admin_box = store.load_mailbox_messages("admin")
    assert {m["subject"] for m in admin_box} >= {"A", "S"}


def test_mail_welcome_and_corrupt_mailbox_recovery(tmp_path):
    store = MailStore(tmp_path / "mail")
    path = store.append_welcome_message(
        "novo@meganet.tectoy.com.br",
        "novo",
        subject="BEM # VINDO",
        body_lines=["LINHA 1", "LINHA 2"],
    )
    messages = store.load_mailbox_messages("novo")
    assert len(messages) == 1
    assert messages[0]["type"] == "welcome"
    assert messages[0]["subject"] == "BEM  VINDO"
    assert messages[0]["body"] == ["LINHA 1", "LINHA 2"]

    path.write_text("{invalid-json", encoding="utf-8")
    assert store.load_mailbox_messages("novo") == []
    assert store.mailbox_panel_notice_count("novo") == 0


def test_mail_parser_supports_split_labels_label_free_and_latin1_glyphs():
    split = MailStore.parse_upload([
        b"PARA\r", b"bob\r", b"ASSUNTO\r", b"OI\r", b"TEXTO\r", b"LINHA 1\rLINHA 2\r",
    ])
    assert split["to"] == "bob"
    assert split["subject"] == "OI"
    assert split["body"] == "LINHA 1\nLINHA 2"

    label_free = MailStore.parse_upload([b"alice\rTESTE\rCORPO DA MENSAGEM\r"])
    assert label_free["to"] == "alice"
    assert label_free["subject"] == "TESTE"
    assert label_free["body"] == "CORPO DA MENSAGEM"

    high = MailStore.parse_upload([b"PARA: bob\rASSUNTO: ICON\rTEXTO: A\xC4B\xE6C\r"])
    assert high["body"].encode("latin-1") == b"A\xC4B\xE6C"
