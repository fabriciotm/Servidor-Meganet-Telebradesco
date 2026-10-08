from __future__ import annotations

import argparse
import datetime as dt
from meganet_server.admin import server as admin_server
from meganet_server.admin import statistics as admin_statistics
from meganet_server.admin import views


def _page_renderer(title: str, body: str) -> bytes:
    return f"TITLE:{title}\n{body}".encode("utf-8")


def _malformed_stats() -> dict:
    return {
        "start_date": dt.date(2026, 10, 1),
        "end_date": dt.date(2026, 10, 4),
        "start_time": dt.time(0, 0),
        "end_time": dt.time(23, 59),
        "service": "all",
        "buckets": [
            {
                "label": "04/10 <teste>",
                "meganet_tx": "invalido",
                "meganet_rx": None,
                "telebradesco_tx": "1024",
                "telebradesco_rx": {},
            }
        ],
        "connections": [
            {
                "service": "meganet",
                "identity": "<usuario>",
                "peer": "127.0.0.1:1",
                "start_display": "04/10 10:00",
                "end_display": "04/10 10:01",
                "duration_seconds": "invalido",
                "tx_bytes": "invalido",
                "rx_bytes": None,
                "avg_bps": "invalido",
                "status": "encerrada",
            },
            {
                "service": "telebradesco",
                "identity": "conta-1",
                "peer": "127.0.0.1:2",
                "start_display": "04/10 11:00",
                "end_display": "-",
                "duration_seconds": 60,
                "tx_bytes": 2048,
                "rx_bytes": 1024,
                "avg_bps": 51,
                "status": "aberta",
            },
        ],
        "events": [
            {
                "service": "meganet",
                "identity": "<usuario>",
                "peer": "127.0.0.1:1",
                "time_display": "04/10 10:00",
                "event": "<open>",
                "tx_bytes": "ruim",
                "rx_bytes": [],
            }
        ],
        "totals": {
            "meganet": {"tx": "ruim", "rx": None},
            "telebradesco": {"tx": 2048, "rx": "1024"},
        },
    }


def test_statistics_render_tolerates_malformed_numeric_fields_and_escapes_text():
    payload = admin_statistics.render_system_statistics(
        {"origin": ["<origem>"]},
        query_statistics=lambda _params: _malformed_stats(),
        page_renderer=_page_renderer,
    ).decode("utf-8")

    assert "TITLE:Estatísticas" in payload
    assert "&lt;usuario&gt;" in payload
    assert "&lt;open&gt;" in payload
    assert "&lt;origem&gt;" in payload
    assert "3 KB" in payload
    assert "invalido" not in payload


def test_access_history_tolerates_malformed_numeric_fields_and_paginates():
    stats = _malformed_stats()
    stats["connections"] = stats["connections"] * 30
    payload = admin_statistics.render_system_access_history(
        {"per_page": ["50"], "page": ["2"], "user": ["<nome>"]},
        query_statistics=lambda _params: stats,
        access_date_range=lambda: (dt.date(2026, 10, 1), dt.date(2026, 10, 4)),
        page_renderer=_page_renderer,
    ).decode("utf-8")

    assert "TITLE:Histórico de Acessos" in payload
    assert "Página 2 de 2" in payload
    assert "&lt;nome&gt;" in payload
    assert "&lt;usuario&gt;" in payload
    assert "invalido" not in payload


def test_admin_views_escape_untrusted_fields_and_handle_invalid_records(monkeypatch):
    class AuthManager:
        def ensure_users(self):
            return {
                "users": {
                    "<admin>'\"": {
                        "role": "admin",
                        "active": True,
                        "created_at": "<created>",
                        "updated_at": "<updated>",
                    },
                    "broken": "not-a-record",
                }
            }

    rendered = views.render_admin_user_management(
        AuthManager(), page_renderer=_page_renderer
    ).decode("utf-8")
    assert "&lt;admin&gt;&#x27;&quot;" in rendered
    assert "<created>" not in rendered
    assert "&lt;created&gt;" in rendered
    assert "not-a-record" not in rendered

    form = views.user_create_form(
        {
            "nome": "<Nome>",
            "nome_meganet": "a'\"b",
            "sexo": "valor-invalido",
            "codigo_acesso": "ABC&123",
        },
        error="<erro>",
    )
    assert "&lt;Nome&gt;" in form
    assert "a&#x27;&quot;b" in form
    assert "&lt;erro&gt;" in form
    assert "value='M' selected" in form

    json_form = views.user_json_form("<key>'", {"x": "<tag>"}, "<erro-json>")
    assert "&lt;key&gt;&#x27;" in json_form
    assert "&lt;tag&gt;" in json_form
    assert "&lt;erro-json&gt;" in json_form


def test_start_admin_server_closes_socket_if_worker_thread_cannot_start(tmp_path, monkeypatch):
    instances = []

    class FakeHTTPServer:
        def __init__(self, address, handler):
            self.server_address = address
            self.RequestHandlerClass = handler
            self.closed = False
            instances.append(self)

        def serve_forever(self):
            return None

        def server_close(self):
            self.closed = True

    class FailingThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(admin_server, "ThreadingHTTPServer", FakeHTTPServer)
    monkeypatch.setattr(admin_server.threading, "Thread", FailingThread)

    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )

    assert admin_server.start_admin_server(args) is None
    assert len(instances) == 1
    assert instances[0].closed is True


def test_start_admin_server_disabled_invalid_routes_and_bind_failure(tmp_path, monkeypatch):
    assert admin_server.start_admin_server(argparse.Namespace(admin_web=False)) is None

    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )
    monkeypatch.setattr(admin_server, "validate_route_table", lambda _handler: ["/missing"])
    assert admin_server.start_admin_server(args) is None

    monkeypatch.setattr(admin_server, "validate_route_table", lambda _handler: [])

    def fail_bind(*_args, **_kwargs):
        raise OSError("bind failed")

    monkeypatch.setattr(admin_server, "ThreadingHTTPServer", fail_bind)
    assert admin_server.start_admin_server(args) is None
