from __future__ import annotations

import json

from meganet_server.admin.meganet import _read_cadastro_db, _write_cadastro_db
from meganet_server.meganet.accounts import CadastroDatabase
from meganet_server.runtime import statistics as stats
from meganet_server.telebradesco.accounts import TelebradescoAccountsError, read_telebradesco_accounts


def test_admin_cadastro_read_survives_invalid_json(tmp_path) -> None:
    path = tmp_path / "cadastros.json"
    path.write_text("{invalid", encoding="utf-8")
    data = _read_cadastro_db(path)
    assert data["schema"] == CadastroDatabase.SCHEMA
    assert data["users"] == {}


def test_admin_cadastro_write_is_atomic_and_roundtrips(tmp_path) -> None:
    path = tmp_path / "nested" / "cadastros.json"
    payload = {"schema": CadastroDatabase.SCHEMA, "users": {"A": {"fields": {}}}}
    _write_cadastro_db(path, payload)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema"] == CadastroDatabase.SCHEMA
    assert "A" in loaded["users"]
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_runtime_statistics_tolerates_corrupt_numeric_totals(tmp_path, monkeypatch) -> None:
    traffic_dir = tmp_path / "traffic"
    access_dir = tmp_path / "access"
    traffic_dir.mkdir()
    access_dir.mkdir()
    monkeypatch.setattr(stats, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic_dir)
    monkeypatch.setattr(stats, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access_dir)
    key = stats._runtime_log_date_key()
    (traffic_dir / f"traffic_{key}.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": key,
        "totals": {
            "meganet": {"tx": "not-a-number", "rx": None},
            "telebradesco": {"tx": 12, "rx": "34"},
        },
        "buckets": [],
        "updated_at": "",
    }), encoding="utf-8")
    totals, buckets = stats._runtime_load_today_traffic()
    assert totals["meganet"] == {"tx": 0, "rx": 0}
    assert totals["telebradesco"] == {"tx": 12, "rx": 34}
    assert buckets == []


def test_invalid_telebradesco_accounts_file_is_backed_up(tmp_path) -> None:
    accounts = tmp_path / "accounts.json"
    content = tmp_path / "accounts_content.json"
    accounts.write_text("{invalid", encoding="utf-8")
    try:
        read_telebradesco_accounts(accounts, accounts_content_file=content, create_if_missing=True)
    except TelebradescoAccountsError as exc:
        assert exc.backup_path is not None
        assert exc.backup_path.exists()
        assert exc.backup_path.read_text(encoding="utf-8") == "{invalid"
    else:
        raise AssertionError("invalid account database should be rejected")
