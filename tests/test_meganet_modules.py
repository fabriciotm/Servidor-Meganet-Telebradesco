from __future__ import annotations

import asyncio

from meganet_server.meganet import accounts, content, mail, protocol, session
from meganet_server.meganet.states import MegaNetState


def test_meganet_protocol_golden_frames_v560():
    assert protocol.encode_server_frame(0x12, b"#ready#\r").hex() == "020812237265616479230d0282"
    assert protocol.encode_server_frame(0, b"#bemvindo#").hex() == "020a002362656d76696e646f2303a4"


def test_meganet_access_code_golden_v560():
    assert accounts.calculate_access_code_suffix("ABCDEFGHIJ") == "B7"
    assert accounts.generate_access_code("ABCDEFGHIJ") == "ABCDEFGHIJB7"
    assert accounts.validate_access_code("ABCDEFGHIJB7") is True


def test_meganet_content_golden_budgets_v560():
    report = content.content_budget_report(content.merge_content_records(None))
    assert {key: value["total_bytes"] for key, value in report.items()} == {
        "tectoy-news": 944,
        "internet": 70,
        "revista": 499,
        "outros": 124,
    }
    assert content.split_line_for_rom("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", 35) == [
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345678",
        "9",
    ]


def test_meganet_mail_parser_golden_v560():
    parsed = mail.MailStore.parse_upload([
        b"PARA: kavera@meganet.tectoy.com.br\r",
        b"ASSUNTO: TESTE\r",
        b"TEXTO: OLA MUNDO\r",
    ])
    assert parsed["to"] == "kavera@meganet.tectoy.com.br"
    assert parsed["subject"] == "TESTE"
    assert parsed["body"] == "OLA MUNDO"


class AckWriter:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False
        self.session: session.MegaNetSession | None = None
        self.drain_count = 0

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        self.drain_count += 1
        assert self.session is not None
        control = protocol.NAK if self.drain_count == 1 else protocol.ACK
        self.session.packet_ack_queue.put_nowait((control, asyncio.get_running_loop().time()))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


async def _exercise_arq(tmp_path):
    writer = AckWriter()
    sess = session.MegaNetSession(
        writer,
        peer="golden-test",
        message_lines=content.DEFAULT_MESSAGE,
        output_dir=tmp_path / "sessions",
        cadastro_db_path=tmp_path / "cadastros.json",
        online_start=False,
        meganet_packet_ack_mode="all",
        meganet_packet_ack_timeout_seconds=0.2,
        meganet_packet_max_retries=3,
        outbound_packet_delay_seconds=0,
    )
    writer.session = sess
    await sess.send_packet_reliable(b"#ready#\r", "golden ARQ")
    return sess, writer


def test_meganet_arq_nak_retransmits_same_seq_then_ack_advances(tmp_path):
    sess, writer = asyncio.run(_exercise_arq(tmp_path))
    expected = protocol.encode_server_frame(0, b"#ready#\r")
    assert writer.writes == [expected, expected]
    assert sess.server_seq == 1
    assert sess.packet_tx_attempts == 2
    assert sess.packet_tx_retransmissions == 1
    assert sess.packet_tx_naks == 1
    assert sess.packet_tx_confirmed == 1
    assert sess.packet_tx_failures == 0


def test_meganet_route_ack_kept_outside_protocol_queue(tmp_path):
    async def run():
        class Writer:
            def is_closing(self): return False
            def close(self): pass
            def write(self, data): pass
            async def drain(self): pass
        sess = session.MegaNetSession(
            Writer(), peer="route-test", message_lines=content.DEFAULT_MESSAGE,
            output_dir=tmp_path / "sessions", cadastro_db_path=tmp_path / "cadastros.json",
            online_start=False,
        )
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        sess.packet_ack_waiting_seq = 7
        sess.route_inbound_wire_data(bytes([protocol.ACK]), q)
        control, _when = sess.packet_ack_queue.get_nowait()
        assert control == protocol.ACK
        assert q.empty()
    asyncio.run(run())


def test_server_reexports_meganet_modular_api():
    from meganet_server import server
    assert server.MegaNetSession is session.MegaNetSession
    assert server.CadastroDatabase is accounts.CadastroDatabase
    assert server.MailStore is mail.MailStore
    assert server.ClientFrameParser is protocol.ClientFrameParser
    assert server.content_budget_report is content.content_budget_report


def test_content_store_round_trip(tmp_path):
    from meganet_server.meganet import content_store
    target = tmp_path / "content.json"
    cfg = content_store.default_content_config()
    cfg["message_lines"] = ["LINHA 1", "LINHA 2"]
    path = content_store.write_content_file(target, cfg)
    loaded = content_store.read_content_file(path, create_if_missing=False)
    assert path == target
    assert loaded["message_lines"] == ["LINHA 1", "LINHA 2"]
    assert loaded["content_records"] == content.merge_content_records(cfg["content_records"])


def test_cadastro_database_registration_and_strict_field_login(tmp_path):
    db = accounts.CadastroDatabase(tmp_path / "cadastros.json")
    key, _path = db.upsert_registration(
        session_id="s1", peer="127.0.0.1:1", profile_name="MAURICIO",
        bootstrap_login="meganet", bootstrap_password="guerta",
        fields={"nome": "Kavera", "nome_meganet": "kavera", "senha": "123abc", "codigo_acesso": "ABCDEFGHIJB7"},
        packet_count=3,
    )
    assert key == "kavera"
    ok = db.validate_meganet_id_credentials(
        nome_meganet="kavera", senha="123ABC", auth_mode="require-known", password_case="case-insensitive"
    )
    assert ok.allowed is True
    assert ok.user_key == "kavera"
    missing = db.validate_meganet_id_credentials(
        nome_meganet="MAURICIO", senha="123ABC", auth_mode="require-known", password_case="case-insensitive"
    )
    assert missing.allowed is False


class CaptureWriter:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False
    def write(self, data: bytes): self.writes.append(bytes(data))
    async def drain(self): pass
    def close(self): self.closed = True
    def is_closing(self): return self.closed


def test_meganet_bootstrap_golden_sequence_without_arq(tmp_path):
    async def run():
        writer = CaptureWriter()
        sess = session.MegaNetSession(
            writer, peer="bootstrap-test", message_lines=content.DEFAULT_MESSAGE,
            output_dir=tmp_path / "sessions", cadastro_db_path=tmp_path / "cadastros.json",
            online_start=True, auto_ready_seen=True, profile_idle_seconds=0.001,
            post_dle_delay_seconds=0.001, meganet_packet_ack_mode="off",
            outbound_packet_delay_seconds=0,
        )
        await sess.start()
        await sess.feed(b"meganet\r")
        await sess.feed(b"guerta\r")
        await sess.feed(b"MAURICIO")
        await asyncio.sleep(0.01)
        await sess.feed(bytes([protocol.DLE]))
        await asyncio.sleep(0.02)
        assert sess.state is MegaNetState.PACKET
        assert sess.state == "PACKET"  # legacy string compatibility
        assert writer.writes[:4] == [b"login:\r", b"password:\r", bytes([protocol.ACK]), bytes([protocol.ACK])]
        assert writer.writes[4] == protocol.encode_server_frame(0, b"#bemvindo#")
        assert writer.writes[5] == protocol.encode_server_frame(1, b"#ready#\r")
        sess.cancel_login_prompt_repeater()
        sess.cancel_bootstrap_watchdog()
    asyncio.run(run())


def test_meganet_state_enum_is_string_compatible():
    assert MegaNetState.WAIT_LOGIN == "WAIT_LOGIN"
    assert str(MegaNetState.WAIT_LOGIN) == "WAIT_LOGIN"
    assert {"WAIT_LOGIN": 1}[MegaNetState.WAIT_LOGIN] == 1
