from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from meganet_server.admin.controllers import dashboard as dashboard_module
from meganet_server.admin.controllers.dashboard import DashboardControllerMixin
from meganet_server.config import default_operational_config
from meganet_server.telebradesco import accounts as t_accounts
from meganet_server.telebradesco import content as t_content


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


def _identity_account() -> tuple[str, dict]:
    key = "1234-1234567-4"
    return key, {
        "nome": "Conta preservada",
        "agencia": "1234",
        "conta": "1234567",
        "digito": "4",
        "senha": "1111",
        "bloqueada": False,
    }


def test_unknown_standalone_content_schema_is_not_silently_migrated_or_rewritten(tmp_path) -> None:
    path = tmp_path / "legacy_content.json"
    raw = t_content.blank_telebradesco_content()
    raw["schema"] = "telebradesco_content_v99"
    raw["conta_corrente"]["ultimos_lancamentos"] = [{"data": "01/01/26", "valor": "11,00"}]
    raw["conta_corrente"]["lancamentos_futuros"] = [
        {"data": "02/01", "historico": "FUTURO", "documento": "1", "valor": "22,00"}
    ]
    original = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    path.write_text(original, encoding="utf-8")

    loaded = t_content.read_telebradesco_content(path, create_if_missing=True)

    assert loaded["schema"] == "telebradesco_content_v5"
    assert path.read_text(encoding="utf-8") == original


def test_missing_identity_store_does_not_destroy_existing_financial_content(tmp_path) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key, _account = _identity_account()
    financial = t_content.blank_telebradesco_content()
    financial["conta_corrente"]["saldo_disponivel"] = "9876,54"
    content_payload = {
        "schema": t_accounts.TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA,
        "accounts": {key: financial},
    }
    content_path.write_text(json.dumps(content_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    before = content_path.read_bytes()

    with pytest.raises(t_accounts.TelebradescoAccountsError, match="accounts.json|identidade|identificação"):
        t_accounts.read_telebradesco_accounts(
            accounts_path,
            accounts_content_file=content_path,
            create_if_missing=True,
        )

    assert not accounts_path.exists()
    assert content_path.read_bytes() == before


def test_invalid_accounts_content_is_backed_up_as_content_not_identity(tmp_path) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key, account = _identity_account()
    accounts_path.write_text(
        json.dumps({"schema": t_accounts.TELEBRADESCO_ACCOUNTS_SCHEMA, "accounts": {key: account}}),
        encoding="utf-8",
    )
    content_path.write_text("{corrompido", encoding="utf-8")

    with pytest.raises(t_accounts.TelebradescoAccountsError) as exc_info:
        t_accounts.read_telebradesco_accounts(
            accounts_path,
            accounts_content_file=content_path,
            create_if_missing=True,
        )

    error = exc_info.value
    assert error.path == content_path
    assert error.backup_path is not None
    assert error.backup_path.name.startswith("accounts_content.json.invalid-")
    assert error.backup_path.read_text(encoding="utf-8") == "{corrompido"
    assert not list(tmp_path.glob("accounts.json.invalid-*.bak"))


def test_dashboard_survives_unavailable_meganet_mail_store(tmp_path, monkeypatch) -> None:
    args = _dashboard_args(tmp_path)
    handler = _DashboardHarness(args)

    class _UnavailableMailStore:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("mail filesystem unavailable")

    monkeypatch.setattr(dashboard_module, "MailStore", _UnavailableMailStore)

    handler._get_dashboard(None)

    assert handler.sent[-1][0] == 200
    body = handler.sent[-1][1].decode("utf-8")
    assert "Correio MegaNet indisponível" in body
    assert "Painel" in body


def test_account_writer_never_reactivates_legacy_content_filename(tmp_path) -> None:
    accounts_path = tmp_path / "accounts.json"
    legacy_path = tmp_path / "legacy_content.json"
    active_content_path = tmp_path / "accounts_content.json"
    key, account = _identity_account()
    financial = t_content.blank_telebradesco_content()
    financial["conta_corrente"]["saldo_disponivel"] = "123,45"
    account["content"] = financial

    t_accounts.write_telebradesco_accounts(
        accounts_path,
        {"accounts": {key: account}},
        accounts_content_file=legacy_path,
    )

    assert active_content_path.exists()
    assert not legacy_path.exists()
    saved = json.loads(active_content_path.read_text(encoding="utf-8"))
    assert saved["accounts"][key]["conta_corrente"]["saldo_disponivel"] == "123,45"
