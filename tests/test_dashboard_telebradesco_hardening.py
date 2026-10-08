from __future__ import annotations

import argparse
import json
import pathlib
import threading

import pytest

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


def _account(key_suffix: str = "1234567", *, saldo: str = "10,00") -> tuple[str, dict]:
    key = f"1234-{key_suffix}-4"
    content = t_content.blank_telebradesco_content()
    content["conta_corrente"]["saldo_disponivel"] = saldo
    return key, {
        "nome": "Conta",
        "agencia": "1234",
        "conta": key_suffix,
        "digito": "4",
        "senha": "1111",
        "bloqueada": False,
        "content": content,
    }


def test_dashboard_surfaces_unavailable_telebradesco_database(tmp_path, monkeypatch) -> None:
    args = _dashboard_args(tmp_path)
    handler = _DashboardHarness(args)

    def fail_accounts(*_args, **_kwargs):
        raise t_accounts.TelebradescoAccountsError("indisponível")

    monkeypatch.setattr("meganet_server.admin.controllers.dashboard.read_telebradesco_accounts", fail_accounts)
    handler._get_dashboard(None)

    assert handler.sent[-1][0] == 200
    assert "Banco de contas indisponível" in handler.sent[-1][1].decode("utf-8")


def test_dashboard_tolerates_partial_runtime_snapshot(tmp_path) -> None:
    class _Runtime:
        def connection_count(self):
            return 0

        def dashboard_snapshot(self):
            return {
                "connections": {"total": "bad", "meganet": None},
                "traffic": {"meganet": {"tx": "bad", "rx": None}},
                "buckets": [None, {"label": "12:00", "meganet_tx": "bad", "telebradesco_rx": "7"}],
                "recent_accesses": [None, {"service": "meganet", "peer": "peer"}],
            }

    args = _dashboard_args(tmp_path)
    args._service_runtime = _Runtime()
    handler = _DashboardHarness(args)

    handler._get_dashboard(None)

    assert handler.sent[-1][0] == 200
    body = handler.sent[-1][1].decode("utf-8")
    assert "Painel" in body
    assert "traffic-svg" in body


def test_legacy_content_writer_uses_independent_temp_files(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy_content.json"
    original_write_text = pathlib.Path.write_text
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def synchronized_write_text(self, *args, **kwargs):
        result = original_write_text(self, *args, **kwargs)
        if self.parent == tmp_path and self.name.endswith(".tmp"):
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(pathlib.Path, "write_text", synchronized_write_text)

    def writer(saldo: str) -> None:
        value = t_content.blank_telebradesco_content()
        value["conta_corrente"]["saldo_disponivel"] = saldo
        try:
            t_content.write_telebradesco_content(path, value)
        except BaseException as exc:  # collected for deterministic concurrency assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(saldo,)) for saldo in ("11,00", "22,00")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["conta_corrente"]["saldo_disponivel"] in {"11,00", "22,00"}
    assert not list(tmp_path.glob("*.tmp"))


def test_content_normalization_discards_malformed_collection_shapes() -> None:
    raw = t_content.blank_telebradesco_content()
    raw["conta_corrente"]["extrato"] = "not-a-list"
    raw["conta_corrente"]["ultimos_lancamentos"] = None
    raw["conta_corrente"]["lancamentos_futuros"] = {"bad": "shape"}
    raw["poupanca"]["resumo"] = "not-a-list"
    raw["poupanca"]["extrato_total"] = None
    raw["investimentos"] = "not-a-list"
    raw["cartoes"] = "not-a-list"

    normalized = t_content.normalize_telebradesco_content(raw)

    assert isinstance(normalized["conta_corrente"]["extrato"], list)
    assert isinstance(normalized["conta_corrente"]["ultimos_lancamentos"], list)
    assert isinstance(normalized["conta_corrente"]["lancamentos_futuros"], list)
    assert isinstance(normalized["poupanca"]["resumo"], list)
    assert isinstance(normalized["poupanca"]["extrato_total"], list)
    assert isinstance(normalized["investimentos"], list)
    assert isinstance(normalized["cartoes"], list)
    # A normalized partial object must still be compilable without changing wire rules.
    assert t_content.compile_telebradesco_content(normalized)["frames"]


def test_accounts_content_is_rolled_back_if_identity_write_fails(tmp_path, monkeypatch) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    old_key, old_account = _account("1234567", saldo="10,00")
    t_accounts.write_telebradesco_accounts(
        accounts_path,
        {"accounts": {old_key: old_account}},
        accounts_content_file=content_path,
    )
    old_accounts_bytes = accounts_path.read_bytes()
    old_content_bytes = content_path.read_bytes()

    new_key, new_account = _account("7654321", saldo="99,00")
    original_atomic = t_accounts._atomic_write_json

    def fail_identity(path: pathlib.Path, value: object) -> None:
        if pathlib.Path(path) == accounts_path:
            raise OSError("simulated identity write failure")
        original_atomic(path, value)

    monkeypatch.setattr(t_accounts, "_atomic_write_json", fail_identity)

    with pytest.raises(OSError, match="identity write failure"):
        t_accounts.write_telebradesco_accounts(
            accounts_path,
            {"accounts": {new_key: new_account}},
            accounts_content_file=content_path,
        )

    assert accounts_path.read_bytes() == old_accounts_bytes
    assert content_path.read_bytes() == old_content_bytes


def test_single_account_legacy_content_migrates_to_split_store(tmp_path) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    legacy_path = tmp_path / "legacy_content.json"
    key, account = _account("1234567", saldo="10,00")
    identity_only = dict(account)
    identity_only.pop("content")
    accounts_path.write_text(
        json.dumps({"schema": t_accounts.TELEBRADESCO_ACCOUNTS_SCHEMA, "accounts": {key: identity_only}}),
        encoding="utf-8",
    )
    legacy = t_content.blank_telebradesco_content()
    legacy["conta_corrente"]["saldo_disponivel"] = "44,00"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = t_accounts.read_telebradesco_accounts(
        accounts_path,
        accounts_content_file=content_path,
        create_if_missing=True,
    )

    assert loaded["accounts"][key]["content"]["conta_corrente"]["saldo_disponivel"] == "44,00"
    assert content_path.exists()
    assert not legacy_path.exists()
    assert list(tmp_path.glob("legacy_content.json.migrated-*.bak"))


def test_migration_rolls_back_content_if_identity_rewrite_fails(tmp_path, monkeypatch) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key, account = _account("1234567", saldo="55,00")
    # Embedded content forces the historical one-file schema to be split.
    accounts_path.write_text(
        json.dumps({"schema": t_accounts.TELEBRADESCO_ACCOUNTS_LEGACY_SCHEMA, "accounts": {key: account}}),
        encoding="utf-8",
    )
    original_atomic = t_accounts._atomic_write_json

    def fail_identity(path: pathlib.Path, value: object) -> None:
        if pathlib.Path(path) == accounts_path:
            raise OSError("simulated migration identity failure")
        original_atomic(path, value)

    monkeypatch.setattr(t_accounts, "_atomic_write_json", fail_identity)

    with pytest.raises(t_accounts.TelebradescoAccountsError, match="Banco de contas/conteúdo Telebradesco inválido"):
        t_accounts.read_telebradesco_accounts(
            accounts_path,
            accounts_content_file=content_path,
            create_if_missing=True,
        )

    assert not content_path.exists()


def test_concurrent_access_updates_preserve_counters_and_content(tmp_path) -> None:
    accounts_path = tmp_path / "accounts.json"
    content_path = tmp_path / "accounts_content.json"
    key, account = _account("1234567", saldo="77,00")
    t_accounts.write_telebradesco_accounts(
        accounts_path,
        {"accounts": {key: account}},
        accounts_content_file=content_path,
    )
    errors: list[str] = []

    def worker(index: int) -> None:
        result = t_accounts.record_telebradesco_account_access(
            accounts_path,
            key,
            peer=f"peer-{index}",
            login_text="MEGABRA123412345674111100001234",
            accounts_content_file=content_path,
        )
        if not result:
            errors.append(str(index))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    loaded = t_accounts.read_telebradesco_accounts(
        accounts_path,
        accounts_content_file=content_path,
    )
    saved = loaded["accounts"][key]
    assert saved["total_accesses"] == 12
    assert len(saved["access_history"]) == 12
    assert saved["content"]["conta_corrente"]["saldo_disponivel"] == "77,00"
    identity_raw = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert all("****" in row["login_text"] for row in identity_raw["accounts"][key]["access_history"])
