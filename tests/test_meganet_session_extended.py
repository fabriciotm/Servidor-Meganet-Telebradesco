from __future__ import annotations

import asyncio
import json

import pytest

from meganet_server.meganet import accounts as m_accounts
from meganet_server.meganet import content as m_content
from meganet_server.meganet import protocol as m_protocol
from meganet_server.meganet import session as m_session
from meganet_server.meganet.states import MegaNetState


class CaptureWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 12345)
        return default


def _client_frame(seq: int, payload: bytes) -> bytes:
    checksum = (len(payload) + seq + sum(payload)) & 0xFFFF
    return (
        bytes([m_protocol.STX, len(payload), seq])
        + payload
        + bytes([checksum & 0xFF, checksum >> 8, m_protocol.ETX])
    )


def _server_payload(frame: bytes) -> bytes:
    assert frame[0] == m_protocol.STX
    return frame[3:3 + frame[1]]


def _make_session(tmp_path, writer: CaptureWriter, **overrides) -> m_session.MegaNetSession:
    params = dict(
        peer="extended-flow",
        message_lines=m_content.DEFAULT_MESSAGE,
        output_dir=tmp_path / "sessions",
        cadastro_db_path=tmp_path / "cadastros.json",
        online_start=False,
        meganet_packet_ack_mode="off",
        outbound_packet_delay_seconds=0,
        post_id_message_delay_seconds=0,
        post_id_close_delay_seconds=0,
        transaction_close_delay_seconds=0,
        mail_store_dir=tmp_path / "mail",
        password_change_store_dir=tmp_path / "password",
        welcome_email_enabled=False,
    )
    params.update(overrides)
    return m_session.MegaNetSession(writer, **params)


def _seed_user(path, *, nome_meganet: str, senha: str, codigo: str) -> None:
    db = m_accounts.CadastroDatabase(path)
    db.upsert_registration(
        session_id=f"seed-{nome_meganet}",
        peer="127.0.0.1:1",
        profile_name="MAURICIO",
        bootstrap_login="meganet",
        bootstrap_password="guerta",
        fields={
            "nome": nome_meganet.title(),
            "nome_meganet": nome_meganet,
            "senha": senha,
            "codigo_acesso": codigo,
        },
        packet_count=3,
    )


@pytest.mark.parametrize(
    ("group", "needle"),
    [
        ("1", b"TECTOY NEWS ONLINE"),
        ("2", b"PORTAL MEGA NET"),
        ("3", b"COMICS - LANCAMENTOS"),
        ("4", b"SERVICOS MEGA NET"),
    ],
)
def test_meganet_unified_group_routes_all_validated_profiles(tmp_path, group, needle):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer)
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, f"#send news {group}#\r".encode("ascii")))
        assert writer.writes[0] == bytes([m_protocol.ACK])
        payloads = [_server_payload(frame) for frame in writer.writes[1:]]
        assert payloads[0] == f"#begin news {group}#\r".encode("ascii")
        assert any(needle in payload for payload in payloads)
        assert payloads[-1] == b"#ready#\r"
    asyncio.run(run())


def test_meganet_registration_success_persists_fields_and_email(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, meganet_auth_mode="require-known")
        sess.state = MegaNetState.PACKET
        seq = 0
        await sess.feed(_client_frame(seq, b"#get cadastro#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#id novousuario SENHA1 SSO#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#begin cadastro#\r")); seq += 1
        fields = [
            b"NOVO USUARIO\r", b"NOVO\r", b"01011990\r", b"M\r",
            b"novousuario\r", b"SENHA1\r", b"KLMNOPQRSTC3\r",
        ]
        for value in fields:
            await sess.feed(_client_frame(seq, value)); seq += 1
        await sess.feed(_client_frame(seq, b"#end cadastro#\r"))
        stored = json.loads((tmp_path / "cadastros.json").read_text(encoding="utf-8"))
        user = stored["users"]["novousuario"]
        assert user["fields"]["nome_meganet"] == "novousuario"
        assert user["fields"]["senha"] == "SENHA1"
        assert user["fields"]["email_meganet"] == "novousuario@meganet.tectoy.com.br"
        assert sess.cadastro_db_saved is True
        assert any(b"SEU E-MAIL MEGA NET" in _server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX)
    asyncio.run(run())


def test_meganet_registration_duplicate_name_rejected_without_overwrite(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="kavera", senha="ORIGINAL", codigo="ABCDEFGHIJB7")
        before = json.loads(cadastro_path.read_text(encoding="utf-8"))
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, meganet_auth_mode="require-known", registration_duplicate_action="reject-message")
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#get cadastro#\r"))
        await sess.feed(_client_frame(1, b"#id kavera QUALQUER SSO#\r"))
        await asyncio.sleep(0)
        after = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert after == before
        assert sess.cadastro_db_blocked is True
        payloads = [_server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX]
        assert any(b"O USUARIO kavera JA EXISTE." in payload for payload in payloads)
    asyncio.run(run())


def test_meganet_registration_allow_update_preserves_key_and_updates_fields(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="kavera", senha="123ABC", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            meganet_auth_mode="require-known",
            registration_duplicate_action="allow-update",
            codigo_acesso_duplicate_action="reject-message",
        )
        sess.state = MegaNetState.PACKET
        seq = 0
        await sess.feed(_client_frame(seq, b"#get cadastro#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#id kavera 123ABC SSO#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#begin cadastro#\r")); seq += 1
        fields = [
            b"KAVERA ATUALIZADO\r", b"KAV\r", b"02021990\r", b"M\r",
            b"kavera\r", b"123ABC\r", b"ABCDEFGHIJB7\r",
        ]
        for value in fields:
            await sess.feed(_client_frame(seq, value)); seq += 1
        await sess.feed(_client_frame(seq, b"#end cadastro#\r"))
        stored = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert list(stored["users"]) == ["kavera"]
        assert stored["users"]["kavera"]["fields"]["nome"] == "KAVERA ATUALIZADO"
        assert stored["users"]["kavera"]["total_sessions"] >= 2
        assert sess.cadastro_db_blocked is False
    asyncio.run(run())


def test_meganet_mail_end_to_end_internal_delivery_download_and_clear(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="alice", senha="A1", codigo="ABCDEFGHIJB7")
        _seed_user(cadastro_path, nome_meganet="bob", senha="B1", codigo="KLMNOPQRSTC3")

        sender_writer = CaptureWriter()
        sender = _make_session(tmp_path, sender_writer, meganet_auth_mode="require-known")
        sender.state = MegaNetState.PACKET
        await sender.feed(_client_frame(0, b"#id alice A1 SSO#\r"))
        await sender.feed(_client_frame(1, b"#get mail#\r"))
        await sender.feed(_client_frame(2, b"#begin mail#\r"))
        await sender.feed(_client_frame(3, b"PARA: bob@meganet.tectoy.com.br\r"))
        await sender.feed(_client_frame(4, b"ASSUNTO: TESTE E2E\r"))
        await sender.feed(_client_frame(5, b"TEXTO: OLA BOB\r"))
        await sender.feed(_client_frame(6, b"#end mail#\r"))

        bob_box = sender.mail_store.mailbox_path("bob@meganet.tectoy.com.br")
        mailbox = json.loads(bob_box.read_text(encoding="utf-8"))
        assert len(mailbox["messages"]) == 1
        assert mailbox["messages"][0]["from"] == "alice@meganet.tectoy.com.br"
        assert mailbox["messages"][0]["subject"] == "TESTE E2E"

        receiver_writer = CaptureWriter()
        receiver = _make_session(tmp_path, receiver_writer, meganet_auth_mode="require-known")
        receiver.state = MegaNetState.PACKET
        await receiver.feed(_client_frame(0, b"#id bob B1 SSO#\r"))
        before = len(receiver_writer.writes)
        await receiver.feed(_client_frame(1, b"#send mail#\r"))
        payloads = [_server_payload(frame) for frame in receiver_writer.writes[before + 1:] if frame and frame[0] == m_protocol.STX]
        assert payloads[0] == b"#begin mail#\r"
        assert any(b"DE: alice@meganet.tectoy.com.br" in payload for payload in payloads)
        assert any(b"TESTE E2E" in payload for payload in payloads)
        assert payloads[-1] == b"#ready#\r"
        mailbox_after = json.loads(bob_box.read_text(encoding="utf-8"))
        assert mailbox_after["messages"] == []
        assert mailbox_after["last_downloaded_count"] == 1
    asyncio.run(run())


def test_meganet_legacy_send_internet_alias_routes_to_internet_group(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer)
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#send internet#\r"))
        payloads = [_server_payload(frame) for frame in writer.writes[1:] if frame and frame[0] == m_protocol.STX]
        assert payloads[0] == b"#begin news 2#\r"
        assert any(b"PORTAL MEGA NET" in payload for payload in payloads)
        assert not any(b"TECTOY NEWS ONLINE" in payload for payload in payloads)
    asyncio.run(run())


def test_meganet_duplicate_access_code_rejected_without_saving_new_user(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="existente", senha="OLD1", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, meganet_auth_mode="require-known")
        sess.state = MegaNetState.PACKET
        seq = 0
        await sess.feed(_client_frame(seq, b"#get cadastro#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#id novo NEW1 SSO#\r")); seq += 1
        await sess.feed(_client_frame(seq, b"#begin cadastro#\r")); seq += 1
        fields = [
            b"NOVO\r", b"NOVO\r", b"01011990\r", b"M\r",
            b"novo\r", b"NEW1\r", b"ABCDEFGHIJB7\r",
        ]
        for value in fields:
            await sess.feed(_client_frame(seq, value)); seq += 1
        await sess.feed(_client_frame(seq, b"#end cadastro#\r"))
        stored = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert "novo" not in stored["users"]
        assert sess.state is MegaNetState.AUTH_REJECTED
        assert sess.cadastro_db_blocked is True
        payloads = [_server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX]
        assert b"ERRO\r" in payloads
        assert b"CODIGO DE ACESSO\r" in payloads
        assert payloads[-1] == b"#ready#\r"
        if sess.close_task:
            sess.close_task.cancel()
            try:
                await sess.close_task
            except asyncio.CancelledError:
                pass
    asyncio.run(run())


def test_meganet_wrong_password_message_rejection_locks_session(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="kavera", senha="CERTO", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            meganet_auth_mode="require-known",
            meganet_reject_action="message-then-ready",
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#id kavera ERRADO SSO#\r"))
        assert sess.state is MegaNetState.AUTH_REJECTED
        assert sess.cadastro_db_blocked is True
        payloads = [_server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX]
        assert b"ERRO\r" in payloads
        assert b"SENHA INVALIDA\r" in payloads
        assert payloads[-2:] == [b"#ready#\r", b"#ready#\r"]
        writes_before = list(writer.writes)
        await sess.feed(_client_frame(1, b"#send news 1#\r"))
        assert writer.writes == writes_before
        if sess.close_task:
            sess.close_task.cancel()
            try:
                await sess.close_task
            except asyncio.CancelledError:
                pass
    asyncio.run(run())


def test_meganet_password_change_rejects_unknown_and_wrong_old_password(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="kavera", senha="CERTO", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, password_change_apply=True, content_profile="troca-senha")
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#senha desconhecido OLD NEW#\r"))
        await sess.feed(_client_frame(1, b"#senha kavera ERRADO NEW#\r"))
        stored = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert stored["users"]["kavera"]["fields"]["senha"] == "CERTO"
        payloads = [_server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX]
        assert b"USUARIO NAO ENCONTRADO\r" in payloads
        assert b"SENHA ATUAL INVALIDA\r" in payloads
    asyncio.run(run())


def test_meganet_mail_download_clear_failure_does_not_invalidate_completed_transfer(tmp_path, monkeypatch):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="bob", senha="B1", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, meganet_auth_mode="require-known")
        sess.mail_store.append_to_mailbox(
            "bob@meganet.tectoy.com.br",
            {
                "from": "admin@meganet.tectoy.com.br",
                "to": "bob@meganet.tectoy.com.br",
                "subject": "TESTE",
                "body": ["MENSAGEM"],
            },
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#id bob B1 SSO#\r"))

        def fail_clear(*args, **kwargs):
            raise OSError("simulated mailbox archive failure")

        monkeypatch.setattr(sess.mail_store, "clear_mailbox_messages", fail_clear)
        await sess.feed(_client_frame(1, b"#send mail#\r"))
        payloads = [_server_payload(frame) for frame in writer.writes if frame and frame[0] == m_protocol.STX]
        assert payloads[-1] == b"#ready#\r"
        assert writer.closed is False
        assert sess.state is MegaNetState.PACKET
    asyncio.run(run())


def test_meganet_default_post_id_panel_reports_mail_then_abril_ready(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="kavera", senha="CERTO", codigo="ABCDEFGHIJB7")
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            meganet_auth_mode="require-known",
            post_id_action="id-body-hash-end-ready2-wait",
            connection_panel_mail_count="auto",
        )
        sess.mail_store.append_to_mailbox(
            "kavera@meganet.tectoy.com.br",
            {
                "from": "admin@meganet.tectoy.com.br",
                "to": "kavera@meganet.tectoy.com.br",
                "subject": "AVISO",
                "body": ["MENSAGEM PENDENTE"],
            },
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#id kavera CERTO SSO#\r"))

        payloads = [
            _server_payload(frame)
            for frame in writer.writes
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads[0] == b"#message begin#\r"
        assert b"CORREIO: 01 MENSAGENS\r" in payloads
        assert b"#message end#\r" in payloads
        assert payloads[-2:] == [b"#abril#\r", b"#ready#\r"]

        mailbox_path = sess.mail_store.mailbox_path("kavera@meganet.tectoy.com.br")
        mailbox = json.loads(mailbox_path.read_text(encoding="utf-8"))
        assert len(mailbox["messages"]) == 1
        assert mailbox.get("panel_notice_count") == 0
        # While the message is still pending, the effective panel count remains 1
        # by design; only the one-shot persisted notice marker was consumed.
        assert sess.mail_store.mailbox_panel_notice_count("kavera@meganet.tectoy.com.br") == 1
    asyncio.run(run())


def test_meganet_fragmented_pre_auth_ath0_gets_numeric_ok_and_force_close(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            raw_hayes_in_data_action="ok-close",
            raw_hayes_close_delay_seconds=0,
        )
        sess.state = MegaNetState.WAIT_LOGIN
        for value in b"+++\rATH0\r":
            await sess.feed(bytes([value]))

        assert b"0\r" in writer.writes
        assert sess.raw_hayes_in_data_seen is True
        assert sess.closed_by_transaction is True
        if sess.close_task:
            await sess.close_task
        assert writer.closed is True
    asyncio.run(run())


def test_meganet_unknown_token_message_ready_mapper_response(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, unknown_token_action="message-ready")
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#probe desconhecido#\r"))

        assert writer.writes[0] == bytes([m_protocol.ACK])
        payloads = [
            _server_payload(frame)
            for frame in writer.writes[1:]
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads == [
            b"#message begin#\r",
            b"TOKEN RECEBIDO\r",
            b"#probe desconhecido#\r",
            b"#message end#\r",
            b"#ready#\r",
        ]
    asyncio.run(run())


def test_meganet_bye_closes_even_if_optional_session_log_save_fails(tmp_path, monkeypatch):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            enable_session_logs=True,
            transaction_close="after-bye",
        )
        sess.state = MegaNetState.PACKET

        async def fail_save_outputs() -> None:
            raise OSError("simulated session-log disk failure")

        monkeypatch.setattr(sess, "save_outputs", fail_save_outputs)
        await sess.feed(_client_frame(0, b"#bye#\r"))

        assert writer.writes[0] == bytes([m_protocol.ACK])
        assert sess.closed_by_transaction is True
        if sess.close_task:
            await sess.close_task
        assert writer.closed is True

    asyncio.run(run())


def test_meganet_empty_mail_download_and_get_mail_paths_stay_ready(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer)
        sess.state = MegaNetState.PACKET

        await sess.feed(_client_frame(0, b"#send mail#\r"))
        payloads = [
            _server_payload(frame)
            for frame in writer.writes[1:]
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads[0] == b"#begin mail#\r"
        assert b"CAIXA POSTAL OK" in payloads[1]
        assert payloads[1].endswith(b"#end mail#\r")
        assert payloads[-1] == b"#ready#\r"

        before = len(writer.writes)
        await sess.feed(_client_frame(1, b"#get mail#\r"))
        assert writer.writes[before] == bytes([m_protocol.ACK])
        assert _server_payload(writer.writes[before + 1]) == b"#ready#\r"

    asyncio.run(run())


def test_meganet_manual_group_routing_uses_selected_profile(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(
            tmp_path,
            writer,
            routing_mode="manual",
            content_profile="revista",
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#send news 1#\r"))
        payloads = [
            _server_payload(frame)
            for frame in writer.writes[1:]
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads[0] == b"#begin news 1#\r"
        assert any(b"COMICS - LANCAMENTOS" in payload for payload in payloads)
        assert not any(b"TECTOY NEWS ONLINE" in payload for payload in payloads)
        assert payloads[-1] == b"#ready#\r"

    asyncio.run(run())
