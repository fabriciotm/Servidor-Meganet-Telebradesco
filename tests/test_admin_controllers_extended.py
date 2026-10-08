from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
import pathlib
import urllib.parse

from meganet_server.admin.auth import AdminAuthManager, password_hash
from meganet_server.admin.controllers.auth_system import AuthSystemControllerMixin
from meganet_server.admin.controllers.settings import SettingsControllerMixin
from meganet_server.admin.controllers.meganet import MegaNetControllerMixin
from meganet_server.admin.controllers.telebradesco import TelebradescoControllerMixin
from meganet_server.config import default_operational_config
from meganet_server.admin.meganet import _read_cadastro_db, _write_cadastro_db
from meganet_server.telebradesco.accounts import read_telebradesco_accounts, write_telebradesco_accounts
from meganet_server.telebradesco.content import blank_telebradesco_content
from meganet_server.meganet.accounts import generate_access_code, meganet_email_for_name


class _Runtime:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request_close_all(self, reason: str) -> int:
        self.reasons.append(reason)
        return 1


class _Context:
    def __init__(self, args: argparse.Namespace, config_path: pathlib.Path, auth_manager=None) -> None:
        self.args = args
        self.config_path = config_path
        self.auth_manager = auth_manager

    def render_user_management(self, notices=None) -> bytes:
        return ("USER-MGMT\n" + "\n".join(notices or [])).encode("utf-8")

    def user_json_form(self, key: str, rec: dict, error: str = "") -> str:
        return f"USER-JSON {key} {error}"

    def user_create_form(self, _args, fields=None, error: str = "") -> str:
        return f"USER-CREATE {error}"


class _Harness(AuthSystemControllerMixin, TelebradescoControllerMixin, MegaNetControllerMixin, SettingsControllerMixin):
    def __init__(self, context: _Context) -> None:
        self.app_context = context
        self.sent: list[tuple[int, bytes, str]] = []
        self.redirects: list[str] = []

    def _send(self, status: int, payload, content_type: str = "text/html; charset=utf-8"):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.sent.append((status, payload, content_type))
        return status, payload, content_type

    def _redirect(self, destination: str):
        self.redirects.append(destination)
        return destination

    def _is_admin_user(self, current_user) -> bool:
        return isinstance(current_user, dict) and current_user.get("role") == "admin"


def _args(tmp_path: pathlib.Path) -> argparse.Namespace:
    values = default_operational_config()
    values.update(
        {
            "config": str(tmp_path / "meganet_config.json"),
            "telebradesco_accounts_file": tmp_path / "telebradesco" / "accounts.json",
            "telebradesco_content_file": tmp_path / "telebradesco" / "accounts_content.json",
            "content_dir": tmp_path / "meganet" / "content",
            "cadastro_db": tmp_path / "meganet" / "cadastros.json",
            "mail_store_dir": tmp_path / "meganet" / "mail",
            "mail_inbox": tmp_path / "meganet" / "mail" / "inbox.json",
            "output_dir": tmp_path / "sessions",
            "password_change_store_dir": tmp_path / "password",
            "active_service": "meganet",
        }
    )
    return argparse.Namespace(**values)


def _parsed(path: str = "/"):
    return urllib.parse.urlparse(path)


def _seed_auth(tmp_path: pathlib.Path, users: dict) -> AdminAuthManager:
    ticks = iter([f"2026-10-04T19:00:{i:02d}+00:00" for i in range(60)])
    manager = AdminAuthManager(tmp_path / "admin_web_users.json", utc_now=lambda: next(ticks))
    manager.write_users({"users": users})
    return manager


def test_system_user_individual_update_cannot_remove_last_active_admin(tmp_path) -> None:
    manager = _seed_auth(
        tmp_path,
        {
            "admin": {
                "username": "admin",
                "role": "admin",
                "active": True,
                "password_hash": password_hash("secret"),
            }
        },
    )
    handler = _Harness(_Context(_args(tmp_path), tmp_path / "config.json", manager))

    handler._post_system_users_update(
        _parsed("/system/users/update"),
        {"username": ["admin"], "role": ["user"], "active": ["false"]},
        {"username": "admin", "role": "admin"},
    )

    assert handler.sent[-1][0] == 400
    assert "ao menos um administrador ativo" in handler.sent[-1][1].decode("utf-8")
    saved = manager.read_users()["users"]["admin"]
    assert saved["role"] == "admin"
    assert saved["active"] is True


def test_system_user_individual_update_allows_admin_change_when_another_admin_exists(tmp_path) -> None:
    manager = _seed_auth(
        tmp_path,
        {
            "admin": {"username": "admin", "role": "admin", "active": True, "password_hash": password_hash("a")},
            "backup": {"username": "backup", "role": "admin", "active": True, "password_hash": password_hash("b")},
        },
    )
    handler = _Harness(_Context(_args(tmp_path), tmp_path / "config.json", manager))

    handler._post_system_users_update(
        _parsed("/system/users/update"),
        {"username": ["admin"], "role": ["user"], "active": ["false"]},
        {"username": "backup", "role": "admin"},
    )

    assert handler.sent[-1][0] == 200
    saved = manager.read_users()["users"]
    assert saved["admin"]["role"] == "user"
    assert saved["admin"]["active"] is False
    assert saved["backup"]["role"] == "admin"
    assert saved["backup"]["active"] is True


def test_telebradesco_block_clear_json_and_delete_persist_split_store(tmp_path) -> None:
    args = _args(tmp_path)
    runtime = _Runtime()
    args._service_runtime = runtime
    args.active_service = "telebradesco"
    account_a = {
        "nome": "A",
        "agencia": "1234",
        "conta": "1234567",
        "digito": "4",
        "senha": "1111",
        "bloqueada": False,
        "content": blank_telebradesco_content(),
    }
    account_b = {
        "nome": "B",
        "agencia": "1234",
        "conta": "7654321",
        "digito": "5",
        "senha": "2222",
        "bloqueada": False,
        "content": blank_telebradesco_content(),
    }
    store = {"accounts": {"1234-1234567-4": account_a, "1234-7654321-5": account_b}}
    write_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        store,
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    handler._post_telebradesco_block(
        _parsed("/telebradesco/block"),
        {"account_key": ["1234-1234567-4"], "blocked": ["true"]},
        {"role": "admin"},
    )
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    assert loaded["accounts"]["1234-1234567-4"]["bloqueada"] is True

    replacement = blank_telebradesco_content()
    replacement["conta_corrente"]["saldo_disponivel"] = "321,09"
    handler._post_telebradesco_save_json(
        _parsed("/telebradesco/save-json"),
        {"account_key": ["1234-1234567-4"], "json_body": [json.dumps(replacement)]},
        {"role": "admin"},
    )
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    assert loaded["accounts"]["1234-1234567-4"]["content"]["conta_corrente"]["saldo_disponivel"] == "321,09"

    handler._post_telebradesco_clear_data(
        _parsed("/telebradesco/clear-data"),
        {"account_key": ["1234-1234567-4"]},
        {"role": "admin"},
    )
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    assert loaded["accounts"]["1234-1234567-4"]["content"] == blank_telebradesco_content()

    handler._post_telebradesco_delete(
        _parsed("/telebradesco/delete"),
        {"account_key": ["1234-1234567-4"]},
        {"role": "admin"},
    )
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    assert set(loaded["accounts"]) == {"1234-7654321-5"}
    assert len(runtime.reasons) == 4

    identity_raw = json.loads(pathlib.Path(args.telebradesco_accounts_file).read_text(encoding="utf-8"))
    assert "content" not in identity_raw["accounts"]["1234-7654321-5"]
    content_raw = json.loads(pathlib.Path(args.telebradesco_content_file).read_text(encoding="utf-8"))
    assert "1234-7654321-5" in content_raw["accounts"]


def test_telebradesco_delete_rejects_removing_only_account(tmp_path) -> None:
    args = _args(tmp_path)
    write_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        {
            "accounts": {
                "1234-1234567-4": {
                    "nome": "A",
                    "agencia": "1234",
                    "conta": "1234567",
                    "digito": "4",
                    "senha": "1111",
                    "bloqueada": False,
                    "content": blank_telebradesco_content(),
                }
            }
        },
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    handler = _Harness(_Context(args, tmp_path / "config.json"))
    handler._post_telebradesco_delete(
        _parsed("/telebradesco/delete"),
        {"account_key": ["1234-1234567-4"]},
        {"role": "admin"},
    )
    assert handler.redirects
    assert "banco%20deve%20manter%20pelo%20menos%20uma%20conta" in handler.redirects[-1]


def test_service_select_persistence_failure_does_not_change_live_service(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.active_service = "meganet"
    runtime = _Runtime()
    args._service_runtime = runtime
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_config_file", fail_write)

    handler._post_service_select(
        _parsed("/service/select"),
        {"active_service": ["telebradesco"]},
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert args.active_service == "meganet"
    assert runtime.reasons == []


def test_content_save_persistence_failure_restores_live_content(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.connection_panel_lines = ["ANTES"]
    args.message_lines = ["MENSAGEM ANTIGA"]
    args.content_records = {}
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    def fail_write(*_args, **_kwargs):
        raise OSError("readonly filesystem")

    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_content_file", fail_write)
    monkeypatch.setattr(
        "meganet_server.admin.controllers.settings.content_budget_report",
        lambda _records: {},
    )

    handler._post_content_save(
        _parsed("/content/save"),
        {"connection_panel_lines": ["DEPOIS"], "message_lines": ["NOVA MENSAGEM"]},
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert args.connection_panel_lines == ["ANTES"]
    assert args.message_lines == ["MENSAGEM ANTIGA"]


def test_meganet_json_editor_rejects_duplicate_identity_fields(tmp_path) -> None:
    args = _args(tmp_path)
    data = {
        "users": {
            "ALFA": {
                "fields": {
                    "nome": "Alfa",
                    "nome_meganet": "ALFA",
                    "senha": "1111",
                    "codigo_acesso": "123456",
                    "email_meganet": "alfa@meganet.tectoy.com.br",
                }
            },
            "BETA": {
                "fields": {
                    "nome": "Beta",
                    "nome_meganet": "BETA",
                    "senha": "2222",
                    "codigo_acesso": "654321",
                    "email_meganet": "beta@meganet.tectoy.com.br",
                }
            },
        }
    }
    _write_cadastro_db(pathlib.Path(args.cadastro_db), data)
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    duplicate = dict(data["users"]["BETA"])
    duplicate["fields"] = dict(duplicate["fields"])
    duplicate["fields"].update({
        "nome_meganet": "ALFA",
        "codigo_acesso": "123456",
        "email_meganet": "alfa@meganet.tectoy.com.br",
    })
    handler._post_user_json_save(
        _parsed("/user/json/save"),
        {"key": ["BETA"], "json_body": [json.dumps(duplicate)]},
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 400
    payload = handler.sent[-1][1].decode("utf-8")
    assert "já existe" in payload or "já cadastrado" in payload
    saved = _read_cadastro_db(pathlib.Path(args.cadastro_db))["users"]["BETA"]["fields"]
    assert saved["nome_meganet"] == "BETA"
    assert saved["codigo_acesso"] == "654321"
    assert saved["email_meganet"] == "beta@meganet.tectoy.com.br"


def test_settings_persistence_failure_restores_changed_managed_values(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.active_service = "meganet"
    args.log_level = "INFO"
    args.admin_port = 19778
    runtime = _Runtime()
    args._service_runtime = runtime
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_config_file", fail_write)

    handler._post_settings(
        _parsed("/settings"),
        {
            "active_service": ["telebradesco"],
            "log_level": ["DEBUG"],
            "admin_port": ["29999"],
            "online_start": ["on"],
            "admin_web": ["on"],
        },
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert args.active_service == "meganet"
    assert args.log_level == "INFO"
    assert args.admin_port == 19778
    assert runtime.reasons == []



def test_auth_system_create_bulk_and_delete_workflow(tmp_path) -> None:
    manager = _seed_auth(
        tmp_path,
        {"admin": {"username": "admin", "role": "admin", "active": True, "password_hash": password_hash("admin")}},
    )
    handler = _Harness(_Context(_args(tmp_path), tmp_path / "config.json", manager))

    handler._post_system_users_create(
        _parsed("/system/users/create"),
        {"username": ["viewer"], "password": ["pw"], "role": ["user"]},
        {"username": "admin", "role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert manager.read_users()["users"]["viewer"]["role"] == "user"

    handler._post_system_users_create(
        _parsed("/system/users/create"),
        {"username": ["viewer"], "password": ["pw2"], "role": ["admin"]},
        {"username": "admin", "role": "admin"},
    )
    assert handler.sent[-1][0] == 400

    handler._post_system_users_bulk(
        _parsed("/system/users/bulk"),
        {"selected": ["viewer"], "action": ["save"], "role__viewer": ["admin"], "active__viewer": ["true"]},
        {"username": "admin", "role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert manager.read_users()["users"]["viewer"]["role"] == "admin"

    handler._post_system_users_delete(
        _parsed("/system/users/delete"),
        {"username": ["admin"]},
        {"username": "viewer", "role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert set(manager.read_users()["users"]) == {"viewer"}

    handler._post_system_users_delete(
        _parsed("/system/users/delete"),
        {"username": ["viewer"]},
        {"username": "viewer", "role": "admin"},
    )
    assert handler.sent[-1][0] == 400


def test_meganet_admin_user_and_mail_workflow(tmp_path) -> None:
    args = _args(tmp_path)
    handler = _Harness(_Context(args, tmp_path / "config.json"))
    code = generate_access_code("ABCDEFGHIJ")
    create_form = {
        "field_nome": ["Usuário Teste"],
        "field_nome_meganet": ["TESTE"],
        "field_senha": ["1234"],
        "field_codigo_acesso": [code],
        "field_email_meganet": [""],
    }
    handler._post_user_create(_parsed("/user/create"), create_form, {"role": "admin"})
    assert handler.sent[-1][0] == 200

    data = _read_cadastro_db(pathlib.Path(args.cadastro_db))
    assert "teste" in data["users"]
    email_addr = meganet_email_for_name("TESTE", args.mail_domain)
    assert data["users"]["teste"]["fields"]["email_meganet"] == email_addr

    handler._get_users(_parsed("/users?created=1"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    handler._get_user(_parsed("/user?key=teste"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    handler._get_access_code(_parsed(f"/access-code?codigo={urllib.parse.quote(code)}"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    assert "Código válido" in handler.sent[-1][1].decode("utf-8")

    handler._post_mail_send(
        _parsed("/mail/send"),
        {"to": [email_addr], "subject": ["AVISO"], "body": ["LINHA 1\nLINHA 2"]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    handler._get_mailboxes(_parsed("/mailboxes?sent=1"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    handler._get_mailbox(_parsed("/mailbox?email=" + urllib.parse.quote(email_addr)), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    assert "AVISO" in handler.sent[-1][1].decode("utf-8")
    handler._get_mailbox_message(
        _parsed("/mailbox/message?email=" + urllib.parse.quote(email_addr) + "&index=0"),
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert "LINHA 1" in handler.sent[-1][1].decode("utf-8")

    handler._post_mailbox_clear(
        _parsed("/mailbox/clear"),
        {"email": [email_addr]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert "1 mensagens removidas" in handler.sent[-1][1].decode("utf-8")

    handler._post_user_delete(
        _parsed("/user/delete"),
        {"key": ["teste"]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert "teste" not in _read_cadastro_db(pathlib.Path(args.cadastro_db))["users"]


def test_telebradesco_get_and_rename_preserves_access_statistics(tmp_path) -> None:
    args = _args(tmp_path)
    args.active_service = "meganet"
    original_key = "1234-1234567-4"
    content = blank_telebradesco_content()
    account = {
        "nome": "Conta Original",
        "agencia": "1234",
        "conta": "1234567",
        "digito": "4",
        "senha": "1111",
        "bloqueada": False,
        "total_accesses": 7,
        "last_access_at": "2026-10-04T12:00:00+00:00",
        "last_peer": "peer-old",
        "access_history": [{"at": "2026-10-04T12:00:00+00:00"}],
        "content": content,
    }
    write_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        {"accounts": {original_key: account}},
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    handler._get_telebradesco(_parsed("/telebradesco"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    handler._get_telebradesco(_parsed("/telebradesco?new=1"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    handler._get_telebradesco(_parsed("/telebradesco?account=missing"), {"role": "admin"})
    assert handler.sent[-1][0] == 404

    form = {
        "original_account_key": [original_key],
        "tele_account_name": ["Conta Renomeada"],
        "tele_agencia": ["1234"],
        "tele_conta": ["7654321"],
        "tele_digito": ["5"],
        "tele_senha": ["2222"],
    }
    handler._post_telebradesco_save(_parsed("/telebradesco/save"), form, {"role": "admin"})
    assert handler.redirects[-1].startswith("/telebradesco?saved=1&account=")
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    new_key = "1234-7654321-5"
    assert set(loaded["accounts"]) == {new_key}
    renamed = loaded["accounts"][new_key]
    assert renamed["total_accesses"] == 7
    assert renamed["last_peer"] == "peer-old"
    assert renamed["access_history"] == [{"at": "2026-10-04T12:00:00+00:00"}]


def test_settings_pages_and_successful_save_reload(tmp_path) -> None:
    args = _args(tmp_path)
    runtime = _Runtime()
    args._service_runtime = runtime
    config_path = tmp_path / "meganet_config.json"
    handler = _Harness(_Context(args, config_path))

    handler._get_settings(_parsed("/settings"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    assert b"Configura" in handler.sent[-1][1]

    handler._post_settings(
        _parsed("/settings"),
        {
            "active_service": ["telebradesco"],
            "log_level": ["INFO"],
            "admin_port": ["19778"],
            "online_start": ["on"],
            "admin_web": ["on"],
            "welcome_email_enabled": ["on"],
        },
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert args.active_service == "telebradesco"
    assert runtime.reasons == ["serviço alterado pelas configurações"]
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["active_service"] == "telebradesco"

    handler._get_content(_parsed("/content"), {"role": "admin"})
    assert handler.sent[-1][0] == 200
    assert b"Conte" in handler.sent[-1][1]

    handler._post_reload(
        _parsed("/reload"),
        {"return_to": ["/settings"]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 200
    assert b"recarregados" in handler.sent[-1][1]


def test_content_file_selection_persistence_failure_restores_active_selection(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    original_content_dir = pathlib.Path(args.content_dir)
    handler = _Harness(_Context(args, tmp_path / "config.json"))
    content_write_called = False

    def fail_config_write(*_args, **_kwargs):
        raise OSError("disk full")

    def track_content_write(*_args, **_kwargs):
        nonlocal content_write_called
        content_write_called = True

    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_config_file", fail_config_write)
    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_content_file", track_content_write)

    handler._post_content_save(
        _parsed("/content/save"),
        {"selected_content_file": ["alternativo.json"]},
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert pathlib.Path(args.content_dir) == original_content_dir
    assert content_write_called is False


def test_meganet_admin_user_create_reports_persistence_failure(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    handler = _Harness(_Context(args, tmp_path / "config.json"))
    code = generate_access_code("ABCDEFGHIJ")

    def fail_write(*_args, **_kwargs):
        raise OSError("readonly filesystem")

    monkeypatch.setattr("meganet_server.admin.controllers.meganet._write_cadastro_db", fail_write)

    handler._post_user_create(
        _parsed("/user/create"),
        {
            "field_nome": ["Falha"],
            "field_nome_meganet": ["FALHA"],
            "field_senha": ["1234"],
            "field_codigo_acesso": [code],
            "field_email_meganet": [""],
        },
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert "persist" in handler.sent[-1][1].decode("utf-8").lower()


def test_meganet_admin_user_mutations_report_persistence_failure(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    code = generate_access_code("ABCDEFGHIJ")
    original = {
        "users": {
            "teste": {
                "user_key": "teste",
                "fields": {
                    "nome": "Teste",
                    "nome_meganet": "TESTE",
                    "senha": "1234",
                    "codigo_acesso": code,
                    "email_meganet": meganet_email_for_name("TESTE", args.mail_domain),
                },
            }
        }
    }
    _write_cadastro_db(pathlib.Path(args.cadastro_db), original)
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    def fail_write(*_args, **_kwargs):
        raise OSError("readonly filesystem")

    monkeypatch.setattr("meganet_server.admin.controllers.meganet._write_cadastro_db", fail_write)

    handler._post_user_save(
        _parsed("/user/save"),
        {"key": ["teste"], "field_nome_meganet": ["TESTE"], "field_senha": ["5678"]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 500

    handler._post_user_json_save(
        _parsed("/user/json/save"),
        {"key": ["teste"], "json_body": [json.dumps(original["users"]["teste"])]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 500

    handler._post_user_delete(
        _parsed("/user/delete"),
        {"key": ["teste"]},
        {"role": "admin"},
    )
    assert handler.sent[-1][0] == 500

    persisted = _read_cadastro_db(pathlib.Path(args.cadastro_db))
    assert persisted["users"]["teste"]["fields"]["senha"] == "1234"


def test_reload_content_selection_persistence_failure_restores_selection(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    original_content_dir = pathlib.Path(args.content_dir)
    handler = _Harness(_Context(args, tmp_path / "config.json"))
    reload_called = False

    def fail_config_write(*_args, **_kwargs):
        raise OSError("disk full")

    def track_reload(*_args, **_kwargs):
        nonlocal reload_called
        reload_called = True
        return tmp_path / "unexpected.json"

    monkeypatch.setattr("meganet_server.admin.controllers.settings.write_config_file", fail_config_write)
    monkeypatch.setattr("meganet_server.admin.controllers.settings.reload_active_meganet_content", track_reload)

    handler._post_reload(
        _parsed("/reload"),
        {"selected_content_file": ["alternativo.json"], "return_to": ["/content"]},
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 500
    assert pathlib.Path(args.content_dir) == original_content_dir
    assert reload_called is False


def test_telebradesco_get_storage_creation_failure_returns_503(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("meganet_server.telebradesco.accounts._atomic_write_json", fail_write)

    handler._get_telebradesco(_parsed("/telebradesco"), {"role": "admin"})

    assert handler.sent[-1][0] == 503
    assert "indispon" in handler.sent[-1][1].decode("utf-8").lower()


def test_meganet_admin_concurrent_creates_share_domain_cadastro_lock(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    db_path = pathlib.Path(args.cadastro_db)
    handler_a = _Harness(_Context(args, tmp_path / "config.json"))
    handler_b = _Harness(_Context(args, tmp_path / "config.json"))

    from meganet_server.admin.controllers import meganet as controller_module

    original_read = controller_module._read_cadastro_db

    def synchronized_read(path):
        data = original_read(path)
        time.sleep(0.05)
        return data

    monkeypatch.setattr(controller_module, "_read_cadastro_db", synchronized_read)

    code_a = generate_access_code("ABCDEFGHIJ")
    code_b = generate_access_code("KLMNOPQRST")

    def create(handler, name, code):
        handler._post_user_create(
            _parsed("/user/create"),
            {
                "field_nome": [name],
                "field_nome_meganet": [name],
                "field_senha": ["1234"],
                "field_codigo_acesso": [code],
                "field_email_meganet": [""],
            },
            {"role": "admin"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(create, handler_a, "USUARIOA", code_a),
            pool.submit(create, handler_b, "USUARIOB", code_b),
        ]
        for future in futures:
            future.result(timeout=10)

    saved = _read_cadastro_db(db_path)["users"]
    assert set(saved) == {"usuarioa", "usuariob"}


def test_telebradesco_json_save_identity_failure_rolls_back_content_and_does_not_close_runtime(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    runtime = _Runtime()
    args._service_runtime = runtime
    args.active_service = "telebradesco"
    account_key = "1234-1234567-4"
    original_content = blank_telebradesco_content()
    write_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        {
            "accounts": {
                account_key: {
                    "nome": "A",
                    "agencia": "1234",
                    "conta": "1234567",
                    "digito": "4",
                    "senha": "1111",
                    "bloqueada": False,
                    "content": original_content,
                }
            }
        },
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    handler = _Harness(_Context(args, tmp_path / "config.json"))

    from meganet_server.telebradesco import accounts as accounts_module

    original_atomic = accounts_module._atomic_write_json
    identity_path = pathlib.Path(args.telebradesco_accounts_file)

    def fail_identity(path, value):
        if pathlib.Path(path) == identity_path:
            raise OSError("identity store unavailable")
        return original_atomic(path, value)

    monkeypatch.setattr(accounts_module, "_atomic_write_json", fail_identity)
    replacement = blank_telebradesco_content()
    replacement["conta_corrente"]["saldo_disponivel"] = "987,65"

    handler._post_telebradesco_save_json(
        _parsed("/telebradesco/save-json"),
        {"account_key": [account_key], "json_body": [json.dumps(replacement)]},
        {"role": "admin"},
    )

    assert handler.redirects
    assert "error=" in handler.redirects[-1]
    assert runtime.reasons == []
    loaded = read_telebradesco_accounts(
        pathlib.Path(args.telebradesco_accounts_file),
        accounts_content_file=pathlib.Path(args.telebradesco_content_file),
    )
    assert loaded["accounts"][account_key]["content"] == original_content


def test_settings_endpoint_ranges_match_typed_runtime_and_persisted_config(tmp_path) -> None:
    args = _args(tmp_path)
    handler = _Harness(_Context(args, tmp_path / "meganet_config.json"))

    handler._post_settings(
        _parsed("/settings"),
        {
            "meganet_port": ["70000"],
            "telebradesco_port": ["0"],
            "meganet_serial_baud": ["1"],
            "telebradesco_serial_baud": ["5000000"],
            "online_start": ["on"],
            "admin_web": ["on"],
        },
        {"role": "admin"},
    )

    assert handler.sent[-1][0] == 200
    assert args.meganet_port == args.app_config.meganet.endpoint.port == 65535
    assert args.telebradesco_port == args.app_config.telebradesco.endpoint.port == 1
    assert args.meganet_serial_baud == args.app_config.meganet.endpoint.serial_baud == 300
    assert args.telebradesco_serial_baud == args.app_config.telebradesco.endpoint.serial_baud == 4_000_000

    persisted = json.loads((tmp_path / "meganet_config.json").read_text(encoding="utf-8"))
    assert persisted["meganet_port"] == 65535
    assert persisted["telebradesco_port"] == 1
    assert persisted["meganet_serial_baud"] == 300
    assert persisted["telebradesco_serial_baud"] == 4_000_000
