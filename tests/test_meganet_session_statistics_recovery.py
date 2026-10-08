from __future__ import annotations

import asyncio
import json

from meganet_server.meganet import accounts as m_accounts
from meganet_server.meganet import content as m_content
from meganet_server.meganet import protocol as m_protocol
from meganet_server.meganet import session as m_session
from meganet_server.meganet.states import MegaNetState
from meganet_server.runtime import statistics as rs
from meganet_server import api
from meganet_server import config as config_module


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


def _seed_user(path, *, nome_meganet: str, senha: str, codigo: str) -> None:
    db = m_accounts.CadastroDatabase(path)
    db.upsert_registration(
        session_id=f"seed-{nome_meganet}",
        peer="127.0.0.1:1",
        profile_name="TESTE",
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


def _make_session(tmp_path, writer: CaptureWriter, **overrides) -> m_session.MegaNetSession:
    params = dict(
        peer="statistics-recovery-flow",
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


def test_invalid_unified_route_profile_keeps_validated_group_default(tmp_path):
    async def run() -> None:
        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer, news_route_map={"2": "not-a-profile"})
        sess.state = MegaNetState.PACKET

        await sess.feed(_client_frame(0, b"#send news 2#\r"))

        payloads = [
            _server_payload(frame)
            for frame in writer.writes[1:]
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads[0] == b"#begin news 2#\r"
        assert any(b"PORTAL MEGA NET" in payload for payload in payloads)
        assert not any(b"TECTOY NEWS ONLINE" in payload for payload in payloads)
        assert payloads[-1] == b"#ready#\r"

    asyncio.run(run())


def test_outgoing_mail_csv_failure_does_not_abort_primary_delivery(tmp_path, monkeypatch):
    async def run() -> None:
        cadastro_path = tmp_path / "cadastros.json"
        _seed_user(cadastro_path, nome_meganet="alice", senha="A1", codigo="ABCDEFGHIJB7")
        _seed_user(cadastro_path, nome_meganet="bob", senha="B1", codigo="KLMNOPQRSTC3")

        writer = CaptureWriter()
        sess = _make_session(tmp_path, writer)
        sess.state = MegaNetState.PACKET
        sess.meganet_id_user = "alice"

        def fail_csv(*_args, **_kwargs):
            raise OSError("csv unavailable")

        monkeypatch.setattr(sess.mail_store, "_write_csv", fail_csv)

        await sess.feed(_client_frame(0, b"#begin mail#\r"))
        await sess.feed(_client_frame(1, b"PARA: bob\rASSUNTO: TESTE\rOLA BOB\r"))
        await sess.feed(_client_frame(2, b"#end mail#\r"))

        payloads = [
            _server_payload(frame)
            for frame in writer.writes
            if frame and frame[0] == m_protocol.STX
        ]
        assert payloads[-1] == b"#ready#\r"
        inbox = sess.mail_store.load_mailbox_messages("bob")
        assert len(inbox) == 1
        assert inbox[0]["subject"] == "TESTE"
        assert inbox[0]["delivery_status"] == "delivered-internal"

        outbox = json.loads(sess.mail_store.outbox_email_path.read_text(encoding="utf-8"))
        assert len(outbox["messages"]) == 1
        assert outbox["messages"][0]["delivery_status"] == "delivered-internal"

    asyncio.run(run())


def test_runtime_traffic_flushes_deltas_accumulated_during_transient_io_failure(tmp_path, monkeypatch):
    access_dir = tmp_path / "access"
    traffic_dir = tmp_path / "traffic"
    access_dir.mkdir()
    traffic_dir.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access_dir)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic_dir)

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    real_atomic_write = rs.atomic_write_json
    attempts = {"count": 0}

    def fail_twice_then_write(path, payload, **kwargs):
        if str(path).endswith(".json") and "traffic_" in str(path) and attempts["count"] < 2:
            attempts["count"] += 1
            raise OSError("temporary disk outage")
        return real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(rs, "atomic_write_json", fail_twice_then_write)

    runtime.record_traffic("meganet", "tx", 10)
    runtime.record_traffic("meganet", "tx", 20)
    runtime.record_traffic("meganet", "tx", 30)

    assert runtime.dashboard_snapshot()["traffic"]["meganet"]["tx"] == 60
    traffic_path = rs._runtime_traffic_log_path()
    persisted = json.loads(traffic_path.read_text(encoding="utf-8"))
    assert persisted["totals"]["meganet"]["tx"] == 60
    assert sum(int(item.get("meganet_tx") or 0) for item in persisted["buckets"]) == 60

    runtime.loop.close()


def test_runtime_traffic_recovery_flushes_previous_day_pending_delta_after_midnight(tmp_path, monkeypatch):
    access_dir = tmp_path / "access"
    traffic_dir = tmp_path / "traffic"
    access_dir.mkdir()
    traffic_dir.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access_dir)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic_dir)

    current = {"value": __import__("datetime").datetime(2026, 10, 4, 23, 59, 50)}
    monkeypatch.setattr(rs, "_runtime_now_local", lambda: current["value"])
    monkeypatch.setattr(
        rs.UnifiedServiceRuntime,
        "_minute_key",
        staticmethod(lambda now=None: (now or current["value"]).strftime("%Y-%m-%d %H:%M")),
    )

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    real_atomic_write = rs.atomic_write_json
    failed = {"done": False}

    def fail_first_traffic_write(path, payload, **kwargs):
        if "traffic_20261004.json" in str(path) and not failed["done"]:
            failed["done"] = True
            raise OSError("disk unavailable before midnight")
        return real_atomic_write(path, payload, **kwargs)

    monkeypatch.setattr(rs, "atomic_write_json", fail_first_traffic_write)

    runtime.record_traffic("meganet", "tx", 40)
    current["value"] = __import__("datetime").datetime(2026, 10, 5, 0, 0, 5)
    runtime.record_traffic("meganet", "tx", 5)

    previous = json.loads((traffic_dir / "traffic_20261004.json").read_text(encoding="utf-8"))
    today = json.loads((traffic_dir / "traffic_20261005.json").read_text(encoding="utf-8"))
    assert previous["totals"]["meganet"]["tx"] == 40
    assert today["totals"]["meganet"]["tx"] == 5

    runtime.loop.close()


def test_invalid_json_news_route_map_is_normalized_before_persistence(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    config_path.write_text(json.dumps({
        "news_route_map": {
            "2": "not-a-profile",
            "4": "mail",
            "9": "revista",
        }
    }), encoding="utf-8")

    args = api.parse_args(["--config", str(config_path)])

    assert args.news_route_map["1"] == "tectoy-news"
    assert args.news_route_map["2"] == "internet"
    assert args.news_route_map["3"] == "revista"
    assert args.news_route_map["4"] == "mail"
    assert args.news_route_map["9"] == "revista"

    persisted = config_module.read_config_file(config_path)
    assert persisted["news_route_map"] == args.news_route_map
