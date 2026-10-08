from __future__ import annotations

import argparse
import json
import pathlib


from meganet_server.admin.controllers.dashboard import DashboardControllerMixin
from meganet_server.config import default_operational_config
from meganet_server.meganet.mail import MailStore


class _DashboardContext:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args


class _DashboardHarness(DashboardControllerMixin):
    def __init__(self, args: argparse.Namespace) -> None:
        self.app_context = _DashboardContext(args)
        self.sent: list[tuple[int, bytes, str]] = []

    def _send(self, status: int, payload, content_type: str = "text/html; charset=utf-8"):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.sent.append((status, payload, content_type))
        return status, payload, content_type


def _dashboard_args(tmp_path: pathlib.Path) -> argparse.Namespace:
    values = default_operational_config()
    values.update(
        {
            "cadastro_db": tmp_path / "meganet" / "cadastros.json",
            "mail_store_dir": tmp_path / "meganet" / "mail",
            "mail_inbox": tmp_path / "meganet" / "mail" / "inbox.json",
            "output_dir": tmp_path / "sessions",
            "telebradesco_accounts_file": tmp_path / "telebradesco" / "accounts.json",
            "telebradesco_content_file": tmp_path / "telebradesco" / "accounts_content.json",
        }
    )
    return argparse.Namespace(**values)


def test_meganet_dashboard_tolerates_mailbox_with_non_object_json_root(tmp_path) -> None:
    args = _dashboard_args(tmp_path)
    store = MailStore(
        pathlib.Path(args.mail_store_dir),
        pathlib.Path(args.mail_inbox),
        mail_domain=args.mail_domain,
        admin_email=args.admin_email,
    )
    broken = store.mailboxes_dir / "broken.json"
    broken.write_text(json.dumps([{"subject": "legacy-root-list"}]), encoding="utf-8")

    handler = _DashboardHarness(args)
    handler._get_dashboard(None, meganet_only=True)

    assert handler.sent[-1][0] == 200
    body = handler.sent[-1][1].decode("utf-8")
    assert "Resumo MegaNet" in body
    assert "Correio" in body


def test_meganet_dashboard_degrades_when_daily_history_directory_is_unavailable(tmp_path, monkeypatch) -> None:
    args = _dashboard_args(tmp_path)
    output_path = pathlib.Path(args.output_dir)
    original_glob = pathlib.Path.glob

    def guarded_glob(self: pathlib.Path, pattern: str):
        if self == output_path and pattern == "meganet_sessions_*.json":
            raise OSError("history unavailable")
        return original_glob(self, pattern)

    monkeypatch.setattr(pathlib.Path, "glob", guarded_glob)
    handler = _DashboardHarness(args)
    handler._get_dashboard(None, meganet_only=True)

    assert handler.sent[-1][0] == 200
    body = handler.sent[-1][1].decode("utf-8")
    assert "Nenhum histórico diário encontrado" in body
    assert "Histórico salvo" in body


def test_cli_preserves_existing_corrupt_config_instead_of_overwriting_it(tmp_path) -> None:
    from meganet_server import cli

    config_path = tmp_path / "meganet_config.json"
    original = b'{"active_service": "meganet", broken'
    config_path.write_bytes(original)

    args = cli.parse_args([
        "--config", str(config_path),
        "--meganet-transport", "off",
        "--telebradesco-transport", "off",
        "--no-admin-web",
        "--content-dir", str(tmp_path / "content.json"),
    ])

    assert args.active_service == "meganet"
    assert config_path.read_bytes() == original


def test_admin_login_does_not_overwrite_concurrent_user_registry_update(tmp_path, monkeypatch) -> None:
    import http.client
    import re
    import threading
    import urllib.parse

    from meganet_server.admin import auth as admin_auth
    from meganet_server.admin import server as admin_server

    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "meganet_config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        manager = httpd.RequestHandlerClass.app_context.auth_manager
        snapshot_taken = threading.Event()
        mutation_done = threading.Event()
        original_create_session = manager.create_session

        def delayed_create_session(username: str) -> str:
            # _post_login reaches create_session only after it has read the user
            # registry.  Give a competing transaction a deterministic window.
            snapshot_taken.set()
            mutation_done.wait(timeout=0.25)
            return original_create_session(username)

        monkeypatch.setattr(manager, "create_session", delayed_create_session)

        def mutate_registry() -> None:
            assert snapshot_taken.wait(timeout=5)
            with manager.locked_users() as data:
                users = data.setdefault("users", {})
                users["auditor"] = {
                    "username": "auditor",
                    "role": "user",
                    "active": True,
                    "created_at": manager.utc_now(),
                    "updated_at": manager.utc_now(),
                    "password_hash": admin_auth.password_hash("auditor-pass"),
                }
                manager.write_users(data)
            mutation_done.set()

        mutator = threading.Thread(target=mutate_registry)
        mutator.start()

        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/login")
        response = conn.getresponse()
        login_html = response.read()
        assert response.status == 200
        csrf_match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert csrf_match
        form = urllib.parse.urlencode({
            "_csrf": csrf_match.group(1).decode("utf-8"),
            "username": "admin",
            "password": "admin",
            "next": "/",
        })
        conn.request(
            "POST",
            "/login",
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        response = conn.getresponse()
        response.read()
        assert response.status == 303
        conn.close()
        mutator.join(timeout=5)
        assert not mutator.is_alive()

        persisted = manager.read_users()
        assert "auditor" in persisted.get("users", {})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_admin_startup_preserves_corrupt_user_registry_and_disables_admin_only(tmp_path) -> None:
    from meganet_server.admin import server as admin_server

    config_path = tmp_path / "meganet_config.json"
    users_path = tmp_path / "admin_web_users.json"
    original = b'{"schema":"admin_web_users_v1","users": broken'
    users_path.write_bytes(original)
    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(config_path),
    )

    httpd = admin_server.start_admin_server(args)

    assert httpd is None
    assert users_path.read_bytes() == original


def test_admin_login_returns_503_if_user_registry_becomes_corrupt_after_startup(tmp_path) -> None:
    import http.client
    import re
    import urllib.parse

    from meganet_server.admin import server as admin_server

    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "meganet_config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/login")
        response = conn.getresponse()
        login_html = response.read()
        assert response.status == 200
        csrf_match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert csrf_match

        manager = httpd.RequestHandlerClass.app_context.auth_manager
        manager.users_path.write_text('{"users": broken', encoding="utf-8")
        form = urllib.parse.urlencode({
            "_csrf": csrf_match.group(1).decode("utf-8"),
            "username": "admin",
            "password": "admin",
            "next": "/",
        })
        conn.request(
            "POST",
            "/login",
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        response = conn.getresponse()
        payload = response.read()
        assert response.status == 503
        assert b"indispon" in payload.lower()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_admin_private_request_redirects_if_registry_becomes_corrupt_after_session_creation(tmp_path) -> None:
    import http.client
    import urllib.parse

    from meganet_server.admin import server as admin_server

    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "meganet_config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        manager = httpd.RequestHandlerClass.app_context.auth_manager
        token = manager.create_session("admin")
        manager.users_path.write_text('{"users": broken', encoding="utf-8")
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            "/",
            headers={"Cookie": f"SMT_ADMIN_SESSION={urllib.parse.quote(token)}"},
        )
        response = conn.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location", "").startswith("/login")
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
