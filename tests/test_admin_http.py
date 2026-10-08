from __future__ import annotations

import http.client
import re
import urllib.parse

from meganet_server import server as s
from meganet_server.admin.router import ADMIN_GET_PRIVATE_ROUTES, validate_route_table


def _request(conn, method, path, *, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    return response, payload


def test_admin_login_stats_and_history_smoke(tmp_path):
    args = s.parse_args([
        "--config", str(tmp_path / "meganet_config.json"),
        "--admin-host", "127.0.0.1",
        "--admin-port", "0",
    ])
    httpd = s.start_admin_server(args)
    assert httpd is not None
    try:
        assert validate_route_table(httpd.RequestHandlerClass) == []
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)

        response, login_html = _request(conn, "GET", "/login")
        assert response.status == 200
        match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert match
        csrf = match.group(1).decode("utf-8")

        form = urllib.parse.urlencode({
            "_csrf": csrf,
            "username": "admin",
            "password": "admin",
            "next": "/",
        })
        response, _ = _request(
            conn,
            "POST",
            "/login",
            body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(form))},
        )
        assert response.status == 303
        cookie = response.getheader("Set-Cookie")
        assert cookie
        session_cookie = cookie.split(";", 1)[0]

        for path in ("/", "/system/stats", "/system/stats/history", "/config.json"):
            response, payload = _request(conn, "GET", path, headers={"Cookie": session_cookie})
            assert response.status == 200, (path, response.status, payload[:200])
            assert payload

        # Todas as rotas GET registradas devem ser despachadas sem erro interno.
        # Algumas exigem parâmetros e podem responder 400/404, o que é esperado.
        for path in ADMIN_GET_PRIVATE_ROUTES:
            response, payload = _request(conn, "GET", path, headers={"Cookie": session_cookie})
            assert response.status < 500, (path, response.status, payload[:300])
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_read_only_admin_user_can_view_but_cannot_mutate(tmp_path):
    from meganet_server.admin.auth import password_hash

    args = s.parse_args([
        "--config", str(tmp_path / "meganet_config.json"),
        "--admin-host", "127.0.0.1",
        "--admin-port", "0",
    ])
    httpd = s.start_admin_server(args)
    assert httpd is not None
    try:
        context = httpd.RequestHandlerClass.app_context
        data = context.auth_manager.ensure_users()
        data["users"]["viewer"] = {
            "username": "viewer",
            "role": "user",
            "active": True,
            "password_hash": password_hash("viewer-secret"),
        }
        context.auth_manager.write_users(data)
        token = context.auth_manager.create_session("viewer")
        session_cookie = f"SMT_ADMIN_SESSION={urllib.parse.quote(token)}"

        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        response, login_html = _request(conn, "GET", "/login")
        assert response.status == 200
        match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert match
        csrf = match.group(1).decode("utf-8")

        response, _ = _request(conn, "GET", "/settings", headers={"Cookie": session_cookie})
        assert response.status == 200
        response, payload = _request(conn, "GET", "/system/users", headers={"Cookie": session_cookie})
        assert response.status == 403
        assert b"Acesso negado" in payload

        previous_service = args.active_service
        form = urllib.parse.urlencode({"_csrf": csrf, "active_service": "telebradesco"})
        response, payload = _request(
            conn,
            "POST",
            "/service/select",
            body=form,
            headers={
                "Cookie": session_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        assert response.status == 403
        assert b"Somente leitura" in payload
        assert args.active_service == previous_service
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
