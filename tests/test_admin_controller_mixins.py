from __future__ import annotations

import ast
from pathlib import Path

from meganet_server.admin.controllers.auth_system import AuthSystemControllerMixin
from meganet_server.admin.controllers.dashboard import DashboardControllerMixin
from meganet_server.admin.controllers.meganet import MegaNetControllerMixin
from meganet_server.admin.controllers.settings import SettingsControllerMixin
from meganet_server.admin.controllers.telebradesco import TelebradescoControllerMixin
from meganet_server.admin.router import (
    ADMIN_GET_PRIVATE_ROUTES,
    ADMIN_GET_PUBLIC_ROUTES,
    ADMIN_POST_PRIVATE_ROUTES,
    validate_route_table,
)
from meganet_server.admin.server import AdminHandler


CONTROLLER_TYPES = (
    AuthSystemControllerMixin,
    MegaNetControllerMixin,
    TelebradescoControllerMixin,
    DashboardControllerMixin,
    SettingsControllerMixin,
)


def test_admin_handler_composes_domain_mixins() -> None:
    for controller in CONTROLLER_TYPES:
        assert issubclass(AdminHandler, controller)
    assert validate_route_table(AdminHandler) == []


def test_every_registered_route_has_one_controller_owner() -> None:
    method_names = set(ADMIN_GET_PUBLIC_ROUTES.values())
    method_names.update(ADMIN_GET_PRIVATE_ROUTES.values())
    method_names.update(ADMIN_POST_PRIVATE_ROUTES.values())
    # login/logout and asset/dispatch methods deliberately remain on AdminHandler.
    local_methods = {
        name for name, value in AdminHandler.__dict__.items()
        if callable(value)
    }
    for method_name in method_names:
        owners = [controller for controller in CONTROLLER_TYPES if method_name in controller.__dict__]
        if method_name in local_methods:
            assert not owners, method_name
        else:
            assert len(owners) == 1, (method_name, owners)


def test_controller_modules_do_not_import_admin_server_facade() -> None:
    controller_dir = Path(__file__).parents[1] / "meganet_server" / "admin" / "controllers"
    violations: list[str] = []
    for path in controller_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith("admin.server") or module == "server":
                    violations.append(f"{path.name}:{node.lineno}:{module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("admin.server"):
                        violations.append(f"{path.name}:{node.lineno}:{alias.name}")
    assert violations == []


def test_admin_composition_module_stays_compact() -> None:
    path = Path(__file__).parents[1] / "meganet_server" / "admin" / "server.py"
    assert len(path.read_text(encoding="utf-8").splitlines()) < 300
    # Domain controllers should not creep back into the composition class.
    domain_methods = {
        name for controller in CONTROLLER_TYPES for name in controller.__dict__
        if name.startswith("_get_") or name.startswith("_post_")
    }
    assert not (domain_methods & set(AdminHandler.__dict__))
