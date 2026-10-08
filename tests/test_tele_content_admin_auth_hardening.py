from __future__ import annotations

import argparse
import json
import pathlib
import threading
import urllib.parse

import pytest

from meganet_server.admin.auth import AdminAuthManager, password_hash
from meganet_server.admin.controllers.auth_system import AuthSystemControllerMixin
from meganet_server.config import default_operational_config
from meganet_server.telebradesco.content import (
    compile_telebradesco_content,
    default_telebradesco_content,
    validate_telebradesco_content,
)


class _Context:
    def __init__(self, args: argparse.Namespace, auth_manager: AdminAuthManager) -> None:
        self.args = args
        self.auth_manager = auth_manager

    def render_user_management(self, notices=None) -> bytes:
        return ("USER-MGMT\n" + "\n".join(notices or [])).encode("utf-8")


class _Harness(AuthSystemControllerMixin):
    def __init__(self, context: _Context) -> None:
        self.app_context = context
        self.sent: list[tuple[int, bytes, str]] = []

    def _send(self, status: int, payload, content_type: str = "text/html; charset=utf-8"):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.sent.append((status, payload, content_type))
        return status, payload, content_type

    def _is_admin_user(self, current_user) -> bool:
        return isinstance(current_user, dict) and current_user.get("role") == "admin"


def _args(tmp_path: pathlib.Path) -> argparse.Namespace:
    values = default_operational_config()
    values["config"] = str(tmp_path / "config.json")
    return argparse.Namespace(**values)


def _parsed(path: str):
    return urllib.parse.urlparse(path)


def _auth_manager(tmp_path: pathlib.Path) -> AdminAuthManager:
    counter = 0
    counter_lock = threading.Lock()

    def now() -> str:
        nonlocal counter
        with counter_lock:
            counter += 1
            return f"2026-10-04T20:00:{counter % 60:02d}+00:00"

    manager = AdminAuthManager(tmp_path / "admin_web_users.json", utc_now=now)
    manager.write_users({
        "users": {
            "admin": {
                "username": "admin",
                "role": "admin",
                "active": True,
                "password_hash": password_hash("admin"),
            }
        }
    })
    return manager


def test_compile_partial_financial_summary_entries_does_not_crash() -> None:
    content = default_telebradesco_content()
    content["conta_corrente"]["ultimos_lancamentos"] = [
        {"data": "13/06/26", "valor": "10,00"},
        "registro-corrompido",
        None,
    ]
    content["poupanca"]["resumo"] = [
        {"data": "13/06/26", "valor": "20,00"},
        123,
    ]

    compiled = compile_telebradesco_content(content)

    assert compiled["frames"]
    assert compiled["section_sizes"]["1"] == 78
    assert compiled["section_sizes"]["A"] >= 78


def test_validate_partial_financial_summary_still_rejects_invalid_row() -> None:
    content = default_telebradesco_content()
    content["conta_corrente"]["ultimos_lancamentos"] = ["registro-corrompido"]

    with pytest.raises(ValueError, match="Últimos Lançamentos"):
        validate_telebradesco_content(content)


def test_compile_rejects_content_over_safe_rom_buffer() -> None:
    content = default_telebradesco_content()
    content["conta_corrente"]["extrato"] = [
        {
            "data": "1111",
            "historico": f"H{i:014d}"[-15:],
            "documento": f"{i:07d}".replace("0", "7"),
            "valor": f"{i:019d}".replace("0", "8"),
        }
        for i in range(1800)
    ]

    with pytest.raises(ValueError, match="buffer seguro da ROM"):
        compile_telebradesco_content(content)


def test_admin_concurrent_user_creates_preserve_all_users(tmp_path, monkeypatch) -> None:
    manager = _auth_manager(tmp_path)
    monkeypatch.setattr(
        "meganet_server.admin.controllers.auth_system._admin_password_hash",
        lambda password: f"hash:{password}",
    )
    count = 20
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        handler = _Harness(_Context(_args(tmp_path), manager))
        try:
            barrier.wait(timeout=5)
            handler._post_system_users_create(
                _parsed("/system/users/create"),
                {
                    "username": [f"user{index:02d}"],
                    "password": ["pw"],
                    "role": ["user"],
                },
                {"username": "admin", "role": "admin"},
            )
            assert handler.sent[-1][0] == 200
        except BaseException as exc:  # collected for deterministic assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    saved = manager.read_users()["users"]
    assert set(saved) == {"admin", *(f"user{index:02d}" for index in range(count))}
    assert json.loads(manager.users_path.read_text(encoding="utf-8"))["schema"] == "admin_web_users_v1"


def test_admin_concurrent_role_updates_do_not_lose_other_user_change(tmp_path, monkeypatch) -> None:
    manager = _auth_manager(tmp_path)
    data = manager.read_users()
    data["users"].update({
        "alpha": {"username": "alpha", "role": "user", "active": True, "password_hash": "hash:a"},
        "beta": {"username": "beta", "role": "user", "active": True, "password_hash": "hash:b"},
    })
    manager.write_users(data)
    monkeypatch.setattr(
        "meganet_server.admin.controllers.auth_system._admin_password_hash",
        lambda password: f"hash:{password}",
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def update(username: str) -> None:
        handler = _Harness(_Context(_args(tmp_path), manager))
        try:
            barrier.wait(timeout=5)
            handler._post_system_users_update(
                _parsed("/system/users/update"),
                {"username": [username], "role": ["admin"], "active": ["true"]},
                {"username": "admin", "role": "admin"},
            )
            assert handler.sent[-1][0] == 200
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=update, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    users = manager.read_users()["users"]
    assert users["alpha"]["role"] == "admin"
    assert users["beta"]["role"] == "admin"


def test_read_legacy_content_returns_normalized_data_when_migration_write_fails(tmp_path, monkeypatch) -> None:
    from meganet_server.telebradesco import content as content_module

    path = tmp_path / "legacy_content.json"
    path.write_text(json.dumps({
        "schema": "telebradesco_content_v4",
        "conta_corrente": {
            "titulo": "CONTA TESTE",
            "saldo": "777,77",
            "ultimos_lancamentos": [],
            "lancamentos_futuros": [],
            "extrato": [],
        },
        "poupanca": {},
        "investimentos": [],
        "cartoes": [],
    }), encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(content_module, "write_telebradesco_content", fail_write)

    loaded = content_module.read_telebradesco_content(path, create_if_missing=True)

    assert loaded["schema"] == "telebradesco_content_v5"
    assert loaded["conta_corrente"]["titulo"] == "CONTA TESTE"
    assert loaded["conta_corrente"]["saldo"] == "777,77"
