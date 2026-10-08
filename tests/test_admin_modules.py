from __future__ import annotations

from meganet_server.admin.auth import (
    ADMIN_WEB_SESSION_COOKIE,
    AdminAuthManager,
    cookie_value,
    normalize_role,
    password_hash,
    password_verify,
)
from meganet_server.admin.router import (
    ADMIN_GET_PRIVATE_ROUTES,
    ADMIN_GET_PUBLIC_ROUTES,
    ADMIN_POST_PRIVATE_ROUTES,
)


def test_admin_password_hash_round_trip():
    encoded = password_hash("segredo")
    assert encoded.startswith("pbkdf2_sha256$")
    assert password_verify("segredo", encoded)
    assert not password_verify("errada", encoded)


def test_admin_auth_store_and_session(tmp_path):
    ticks = iter(["2026-10-02T09:00:00+00:00"] * 20)
    manager = AdminAuthManager(tmp_path / "admin_web_users.json", utc_now=lambda: next(ticks))
    data = manager.ensure_users()
    assert "admin" in data["users"]
    assert normalize_role(data["users"]["admin"]["role"]) == "admin"

    token = manager.create_session("admin")
    user = manager.user_from_session_token(token)
    assert user == {"username": "admin", "role": "admin", "active": True}
    manager.destroy_session(token)
    assert manager.user_from_session_token(token) is None


def test_admin_cookie_parser():
    header = f"theme=dark; {ADMIN_WEB_SESSION_COOKIE}=abc%20123; x=y"
    assert cookie_value(header, ADMIN_WEB_SESSION_COOKIE) == "abc 123"


def test_admin_route_tables_are_explicit_and_disjoint():
    assert ADMIN_GET_PUBLIC_ROUTES["/login"] == "_get_login"
    assert ADMIN_GET_PRIVATE_ROUTES["/system/stats"] == "_get_system_stats"
    assert ADMIN_GET_PRIVATE_ROUTES["/system/stats/history"] == "_get_system_stats_history"
    assert ADMIN_POST_PRIVATE_ROUTES["/telebradesco/save"] == "_post_telebradesco_save"
    assert not (set(ADMIN_GET_PUBLIC_ROUTES) & set(ADMIN_GET_PRIVATE_ROUTES))
