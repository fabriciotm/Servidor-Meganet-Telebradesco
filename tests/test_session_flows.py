from __future__ import annotations

import asyncio
import json

from meganet_server.meganet import accounts as m_accounts
from meganet_server.meganet import content as m_content
from meganet_server.meganet import protocol as m_protocol
from meganet_server.meganet import session as m_session
from meganet_server.meganet.states import MegaNetState
from meganet_server.telebradesco import accounts as t_accounts
from meganet_server.telebradesco import content as t_content
from meganet_server.telebradesco import protocol as t_protocol
from meganet_server.telebradesco import session as t_session
from meganet_server.telebradesco.states import TelebradescoMode, TelebradescoStage


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


def _seed_meganet_user(path, *, nome_meganet: str = "kavera", senha: str = "123abc") -> None:
    db = m_accounts.CadastroDatabase(path)
    db.upsert_registration(
        session_id="seed",
        peer="127.0.0.1:1",
        profile_name="MAURICIO",
        bootstrap_login="meganet",
        bootstrap_password="guerta",
        fields={
            "nome": "Kavera",
            "nome_meganet": nome_meganet,
            "senha": senha,
            "codigo_acesso": "ABCDEFGHIJB7",
        },
        packet_count=3,
    )


def _make_meganet_session(tmp_path, writer: CaptureWriter, **overrides) -> m_session.MegaNetSession:
    params = dict(
        peer="session-flow",
        message_lines=m_content.DEFAULT_MESSAGE,
        output_dir=tmp_path / "sessions",
        cadastro_db_path=tmp_path / "cadastros.json",
        online_start=True,
        auto_ready_seen=True,
        profile_idle_seconds=0.001,
        post_dle_delay_seconds=0.001,
        meganet_packet_ack_mode="off",
        outbound_packet_delay_seconds=0,
        mail_store_dir=tmp_path / "mail",
        password_change_store_dir=tmp_path / "password",
        welcome_email_enabled=False,
    )
    params.update(overrides)
    return m_session.MegaNetSession(writer, **params)


async def _bootstrap_meganet(sess: m_session.MegaNetSession) -> None:
    await sess.start()
    await sess.feed(b"meganet\r")
    await sess.feed(b"guerta\r")
    await sess.feed(b"MAURICIO")
    await asyncio.sleep(0.01)
    await sess.feed(bytes([m_protocol.DLE]))
    await asyncio.sleep(0.02)
    assert sess.state is MegaNetState.PACKET
    sess.cancel_login_prompt_repeater()
    sess.cancel_bootstrap_watchdog()


def test_meganet_full_known_user_login_id_ready_bye_persists_access(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_meganet_user(cadastro_path)
        identities: list[str] = []
        writer = CaptureWriter()
        sess = _make_meganet_session(
            tmp_path,
            writer,
            meganet_auth_mode="require-known",
            post_id_action="ready",
            access_identity_callback=identities.append,
        )

        await _bootstrap_meganet(sess)
        assert writer.writes[:6] == [
            b"login:\r",
            b"password:\r",
            bytes([m_protocol.ACK]),
            bytes([m_protocol.ACK]),
            m_protocol.encode_server_frame(0, b"#bemvindo#"),
            m_protocol.encode_server_frame(1, b"#ready#\r"),
        ]

        await sess.feed(_client_frame(0, b"#id kavera 123ABC SSO#\r"))
        assert sess.meganet_auth_result is not None
        assert sess.meganet_auth_result.allowed is True
        assert sess.meganet_auth_result.user_key == "kavera"
        assert identities == ["kavera"]
        assert writer.writes[-2] == bytes([m_protocol.ACK])
        assert writer.writes[-1] == m_protocol.encode_server_frame(2, b"#ready#\r")

        stored = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert stored["users"]["kavera"]["total_sessions"] == 2
        assert stored["users"]["kavera"]["history"][-1]["source"] == "meganet-id-auth"

        await sess.feed(_client_frame(1, b"#bye#\r"))
        await asyncio.sleep(0)
        assert writer.writes[-1] == bytes([m_protocol.ACK])
        assert writer.closed is True
        assert sess.closed_by_transaction is True

    asyncio.run(run())


def test_meganet_strict_unknown_id_sends_frame_ack_then_auth_nak_and_closes(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_meganet_session(
            tmp_path,
            writer,
            online_start=False,
            meganet_auth_mode="require-known",
            meganet_reject_action="nak-close",
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(7, b"#id desconhecido ABC123 SSO#\r"))
        assert writer.writes == [bytes([m_protocol.ACK]), bytes([m_protocol.NAK])]
        assert writer.closed is True
        assert sess.cadastro_db_blocked is True
        assert sess.meganet_auth_result is not None
        assert sess.meganet_auth_result.reason == "unknown_nome_meganet_strict"

    asyncio.run(run())


def test_meganet_bootstrap_tolerates_one_high_bit_byte_then_fails_closed(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_meganet_session(
            tmp_path,
            writer,
            meganet_bootstrap_noise_tolerance=1,
            meganet_bootstrap_noise_window_seconds=10,
        )
        await sess.start()
        assert sess.state is MegaNetState.WAIT_LOGIN
        await sess.feed(b"\x80")
        assert writer.closed is False
        assert sess.state is MegaNetState.WAIT_LOGIN
        assert sess.bootstrap_noise_total == 1
        await sess.feed(b"\x81")
        assert sess.bootstrap_terminate_requested is True
        assert writer.closed is True
        sess.cancel_login_prompt_repeater()
        sess.cancel_bootstrap_watchdog()

    asyncio.run(run())


def test_meganet_news_group_request_has_begin_end_and_final_ready(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_meganet_session(tmp_path, writer, online_start=False)
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#send news 2#\r"))
        assert writer.writes[0] == bytes([m_protocol.ACK])
        frames = writer.writes[1:]
        assert frames[0] == m_protocol.encode_server_frame(0, b"#begin news 2#\r")
        assert any(b"#end#\r" in frame for frame in frames)
        assert frames[-1] == m_protocol.encode_server_frame(len(frames) - 1, b"#ready#\r")
        assert sess.server_seq == len(frames)

    asyncio.run(run())


def _tele_bitrev(data: bytes) -> bytes:
    return bytes(t_protocol._tele_reverse_bits8(value) for value in data)


def _tele_login_wire(login_text: str) -> bytes:
    logical = b"\xFF\xC0\xC2" + login_text.encode("ascii") + b"\x10\x03"
    return _tele_bitrev(logical)


def _tele_ack_wire(code: int) -> bytes:
    return _tele_bitrev(bytes([0x00, 0x00, code & 0xFF]))


def _seed_telebradesco_account(tmp_path, *, blocked: bool = False):
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key = t_accounts.telebradesco_account_key("2924", "0023456", "7")
    store = t_accounts.default_telebradesco_accounts()
    store["accounts"][key] = {
        "nome": "CONTA TESTE",
        "agencia": "2924",
        "conta": "0023456",
        "digito": "7",
        "senha": "1234",
        "bloqueada": blocked,
        "content": t_content.default_telebradesco_content(),
    }
    t_accounts.write_telebradesco_accounts(
        accounts_path,
        store,
        accounts_content_file=content_path,
    )
    return accounts_path, content_path, key


def _make_tele_session(tmp_path, writer, *, blocked: bool = False, error_hold_seconds: float = 0.0, callback=None):
    accounts_path, content_path, key = _seed_telebradesco_account(tmp_path, blocked=blocked)
    sess = t_session.TelebradescoSession(
        writer,
        peer="tele-flow",
        accounts_file=accounts_path,
        accounts_content_file=content_path,
        error_hold_seconds=error_hold_seconds,
        access_identity_callback=callback,
    )
    return sess, accounts_path, content_path, key


async def _feed_tele_login(sess: t_session.TelebradescoSession, login_text: str) -> None:
    await sess.start()
    await sess.feed(b"AT+FRH=3\r")
    assert sess.stage is TelebradescoStage.AWAIT_LOGIN_FTH
    await sess.feed(b"AT+FTH=3\r")
    assert sess.mode is TelebradescoMode.LOGIN_FRAME
    await sess.feed(_tele_login_wire(login_text))
    assert sess.mode is TelebradescoMode.LOGIN_CONFIRM
    assert sess.stage is TelebradescoStage.AWAIT_LOGIN_CONFIRM


async def _confirm_tele_login(sess: t_session.TelebradescoSession) -> None:
    confirm = bytes.fromhex("FF C8 C1 00 52 13 01 01 00")
    await sess.feed(_tele_bitrev(confirm))
    assert sess.mode is TelebradescoMode.COMMAND
    assert sess.stage is TelebradescoStage.AWAIT_HELLO_FRM


def test_telebradesco_successful_session_retries_first_frame_then_completes(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        identities: list[str] = []
        sess, accounts_path, _content_path, key = _make_tele_session(
            tmp_path,
            writer,
            error_hold_seconds=5.0,
            callback=identities.append,
        )
        login = "MEGABRA292400234567123412345678"
        await _feed_tele_login(sess, login)
        assert sess.auth_result is not None and sess.auth_result["status"] == "ok"
        assert sess.auth_wire_code == "999"
        assert identities == ["CONTA TESTE"]
        assert len(sess.frames) == 4
        assert writer.writes[-1] == b"1\r"

        await _confirm_tele_login(sess)
        await sess.feed(b"AT+FRM=48\r")
        assert sess.stage is TelebradescoStage.AWAIT_HELLO_FTH
        await sess.feed(b"AT+FTH=3\r")
        assert sess.mode is TelebradescoMode.ACK3
        assert sess.ack_context == "hello"
        await sess.feed(_tele_ack_wire(0x21))
        assert sess.stage is TelebradescoStage.AWAIT_DATA_FRM
        assert sess.frame_index == 0 and sess.sequence == 2

        expected_first = t_protocol._tele_class1_wire(sess.frames[0], 2)
        before = len(writer.writes)
        await sess.feed(b"AT+FRM=48\r")
        assert b"".join(writer.writes[before:]).endswith(b"1\r" + expected_first)
        await sess.feed(b"AT+FTH=3\r")
        await sess.feed(_tele_ack_wire(0x22))
        assert sess.frame_index == 0 and sess.sequence == 2
        assert sess.stage is TelebradescoStage.AWAIT_DATA_FRM

        before = len(writer.writes)
        await sess.feed(b"AT+FRM=48\r")
        assert b"".join(writer.writes[before:]).endswith(b"1\r" + expected_first)
        await sess.feed(b"AT+FTH=3\r")
        await sess.feed(_tele_ack_wire(0x21))
        assert sess.frame_index == 1 and sess.sequence == 3

        while sess.frame_index < len(sess.frames):
            index = sess.frame_index
            seq = sess.sequence
            expected = t_protocol._tele_class1_wire(sess.frames[index], seq)
            before = len(writer.writes)
            await sess.feed(b"AT+FRM=48\r")
            assert b"".join(writer.writes[before:]).endswith(b"1\r" + expected)
            await sess.feed(b"AT+FTH=3\r")
            await sess.feed(_tele_ack_wire(0x21))

        assert sess.stage is TelebradescoStage.AWAIT_TERMINAL
        assert sess.frame_index == len(sess.frames)
        before = len(writer.writes)
        await sess.feed(b"AT+FRM=48\r")
        assert b"".join(writer.writes[before:]).endswith(b"3\r")

        # Successful sessions must not use the authentication-error hold.
        await asyncio.wait_for(sess.handle_command("ATH0"), timeout=0.10)
        assert sess.stage is TelebradescoStage.FINISHED
        if sess.close_task:
            sess.close_task.cancel()
            try:
                await sess.close_task
            except asyncio.CancelledError:
                pass

        stored = json.loads(accounts_path.read_text(encoding="utf-8"))
        assert stored["accounts"][key]["total_accesses"] == 1
        assert stored["accounts"][key]["access_history"][-1]["login_text"] == "MEGABRA292400234567****12345678"

    asyncio.run(run())


def test_telebradesco_blocked_account_arms_native_020_status_only_frames(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess, _accounts_path, _content_path, _key = _make_tele_session(tmp_path, writer, blocked=True)
        await _feed_tele_login(sess, "MEGABRA292400234567123412345678")
        assert sess.auth_result is not None
        assert sess.auth_result["status"] == "blocked"
        assert sess.auth_wire_code == "020"
        expected = t_content.compile_telebradesco_status("020", None, block_mode="native-020")
        assert sess.frames == expected["frames"]
        assert sess.mode is TelebradescoMode.LOGIN_CONFIRM

    asyncio.run(run())


def test_telebradesco_wrong_password_maps_to_011_and_keeps_transport_connected(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess, _accounts_path, _content_path, _key = _make_tele_session(tmp_path, writer)
        await _feed_tele_login(sess, "MEGABRA292400234567999912345678")
        assert sess.auth_result is not None
        assert sess.auth_result["status"] == "invalid_password"
        assert sess.auth_wire_code == "011"
        assert writer.closed is False
        assert writer.writes[-1] == b"1\r"
        assert sess.frames == t_content.compile_telebradesco_status("011", None, block_mode="native-020")["frames"]

    asyncio.run(run())


def test_telebradesco_invalid_agency_and_missing_account_map_native_codes(tmp_path):
    async def one(login: str, expected_status: str, expected_code: str) -> None:
        writer = CaptureWriter()
        sess, _accounts_path, _content_path, _key = _make_tele_session(tmp_path / expected_code, writer)
        await _feed_tele_login(sess, login)
        assert sess.auth_result is not None
        assert sess.auth_result["status"] == expected_status
        assert sess.auth_wire_code == expected_code

    async def run() -> None:
        await one("MEGABRA292500234567123412345678", "invalid_agency", "040")
        await one("MEGABRA292499999997123412345678", "account_not_found", "042")

    asyncio.run(run())


def test_meganet_bad_client_checksum_is_nak_without_state_transition(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_meganet_session(tmp_path, writer, online_start=False)
        sess.state = MegaNetState.PACKET
        raw = bytearray(_client_frame(3, b"#bye#\r"))
        raw[-2] ^= 0x01
        await sess.feed(bytes(raw))
        assert writer.writes == [bytes([m_protocol.NAK])]
        assert writer.closed is False
        assert sess.state is MegaNetState.PACKET

    asyncio.run(run())


def test_meganet_password_change_token_updates_known_user_when_enabled(tmp_path):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_meganet_user(cadastro_path)
        writer = CaptureWriter()
        sess = _make_meganet_session(
            tmp_path,
            writer,
            online_start=False,
            password_change_apply=True,
            content_profile="troca-senha",
        )
        sess.state = MegaNetState.PACKET
        await sess.feed(_client_frame(0, b"#senha kavera 123ABC NOVA12#\r"))
        stored = json.loads(cadastro_path.read_text(encoding="utf-8"))
        assert stored["users"]["kavera"]["fields"]["senha"] == "NOVA12"
        assert writer.writes[0] == bytes([m_protocol.ACK])
        assert any(b"SENHA ALTERADA" in frame for frame in writer.writes[1:])
        events = json.loads((tmp_path / "password" / "password_change_probe.json").read_text(encoding="utf-8"))
        kinds = [entry.get("kind") for entry in events["events"]]
        assert kinds[-2:] == ["senha_token", "password_applied"]

    asyncio.run(run())


def test_telebradesco_malformed_login_maps_to_001_without_closing_transport(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess, _accounts_path, _content_path, _key = _make_tele_session(tmp_path, writer)
        await sess.start()
        await sess.feed(b"AT+FRH=3\r")
        await sess.feed(b"AT+FTH=3\r")
        await sess.feed(_tele_login_wire("MEGABRAINVALID"))
        assert sess.auth_result is not None
        assert sess.auth_result["status"] == "malformed_login"
        assert sess.auth_wire_code == "001"
        assert sess.frames == t_content.compile_telebradesco_status("001", None, block_mode="native-020")["frames"]
        assert writer.closed is False
        assert sess.mode is TelebradescoMode.LOGIN_CONFIRM

    asyncio.run(run())
