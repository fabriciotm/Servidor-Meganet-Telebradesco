from __future__ import annotations

import inspect

from meganet_server import server
from meganet_server.admin.router import validate_route_table
from meganet_server.admin.server import (
    AdminApplicationContext,
    AdminHandler,
    html_page,
    start_admin_server,
)


def _args(tmp_path, name: str):
    return server.parse_args([
        "--config", str(tmp_path / name / "meganet_config.json"),
        "--admin-host", "127.0.0.1",
        "--admin-port", "0",
    ])


def test_admin_handler_is_top_level_and_route_complete() -> None:
    assert AdminHandler.__qualname__ == "AdminHandler"
    assert validate_route_table(AdminHandler) == []


def test_admin_contexts_are_isolated(tmp_path) -> None:
    first = AdminApplicationContext.create(_args(tmp_path, "one"))
    second = AdminApplicationContext.create(_args(tmp_path, "two"))
    assert first is not second
    assert first.args is not second.args
    assert first.config_path != second.config_path
    assert first.auth_manager is not second.auth_manager
    assert first.auth_manager.users_path != second.auth_manager.users_path


def test_admin_factory_is_compact_and_no_longer_owns_controllers() -> None:
    source = inspect.getsource(start_admin_server)
    assert "class Handler" not in source
    assert "class BoundAdminHandler" in source
    assert len(source.splitlines()) < 50


def test_legacy_admin_exports_still_resolve() -> None:
    import meganet_server.admin.server as admin_server

    assert server.start_admin_server is admin_server.start_admin_server
    assert server.html_page is html_page
    assert server.AdminApplicationContext is AdminApplicationContext
    assert server.AdminHandler is AdminHandler
    assert server.refresh_app_config is not None
