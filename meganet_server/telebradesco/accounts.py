from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import pathlib
import re
import shutil
import threading
import tempfile
from typing import Callable, Iterable, Optional, TypeVar

from ..common import utc_now_iso
from .content import (
    blank_telebradesco_content, validate_telebradesco_content,
    _tele_digits, _tele_normalize_card_identifier,
)

TELEBRADESCO_ROOT_DIR = pathlib.Path("telebradesco")
DEFAULT_TELEBRADESCO_ACCOUNTS_FILE = TELEBRADESCO_ROOT_DIR / "accounts.json"
DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE = TELEBRADESCO_ROOT_DIR / "accounts_content.json"
DEFAULT_TELEBRADESCO_LEGACY_CONTENT_FILE = TELEBRADESCO_ROOT_DIR / "legacy_content.json"  # migration source only

def normalize_telebradesco_accounts_content_path(value: object) -> pathlib.Path:
    """Return the active per-account content path.

    Configurations created by older versions used ``legacy_content.json``. The
    active filename is now ``accounts_content.json``; the old sibling remains a
    migration source and is archived after a successful conversion.
    """
    path = pathlib.Path(value or DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE)
    if path.name.casefold() == "legacy_content.json":
        return path.with_name("accounts_content.json")
    return path
TELEBRADESCO_ACCOUNTS_SCHEMA = "telebradesco_accounts_v2"
TELEBRADESCO_ACCOUNTS_LEGACY_SCHEMA = "telebradesco_accounts_v1"
TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA = "telebradesco_accounts_content_v1"
TELEBRADESCO_AUTH_STATUS = {
    "ok": "999",
    "malformed_login": "001",
    "accounts_unavailable": "001",
    "missing_password": "010",
    "invalid_password": "011",
    "blocked": "020",
    "invalid_agency": "040",
    "account_not_found": "042",
}
TELEBRADESCO_DATA_CARTAO_DIGITS = 4
TELEBRADESCO_LOGIN_REMAINING_DIGITS = 8
_TELE_ACCOUNTS_LOCK = threading.RLock()
_TeleUpdateResult = TypeVar("_TeleUpdateResult")
class TelebradescoAccountsError(ValueError):
    """Account identity/content databases could not be trusted."""

    def __init__(self, message: str, *, path: Optional[pathlib.Path] = None, backup_path: Optional[pathlib.Path] = None) -> None:
        super().__init__(message)
        self.path = path
        self.backup_path = backup_path
def _backup_invalid_telebradesco_file(path: pathlib.Path) -> Optional[pathlib.Path]:
    if not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    stamp = _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.invalid-{stamp}-{stat.st_mtime_ns}.bak")
    try:
        if not backup.exists():
            shutil.copy2(path, backup)
        return backup
    except OSError as exc:
        logging.error("TELE_FILE_BACKUP_FAILED path=%s backup=%s error=%s", path, backup, exc)
        return None
def _backup_telebradesco_migration_file(path: pathlib.Path, label: str) -> Optional[pathlib.Path]:
    if not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-{label}-{stamp}-{stat.st_mtime_ns}.bak")
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError as exc:
        logging.error("TELE_MIGRATION_BACKUP_FAILED path=%s backup=%s error=%s", path, backup, exc)
        return None
def _archive_legacy_telebradesco_content(path: pathlib.Path) -> Optional[pathlib.Path]:
    if not path.exists() or not path.is_file():
        return None
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}.migrated-{stamp}.bak")
    suffix = 1
    while destination.exists():
        destination = path.with_name(f"{path.name}.migrated-{stamp}-{suffix}.bak")
        suffix += 1
    try:
        os.replace(path, destination)
        logging.info("TELE_LEGACY_CONTENT_ARCHIVED old=%s backup=%s", path, destination)
        return destination
    except OSError as exc:
        logging.warning("TELE_LEGACY_CONTENT_ARCHIVE_FAILED path=%s error=%s", path, exc)
        return None
def telebradesco_account_key(agencia: object, conta: object, digito: object) -> str:
    return (
        f"{_tele_digits(agencia, 4, 'Agência')}-"
        f"{_tele_digits(conta, 7, 'Conta corrente')}-"
        f"{_tele_digits(digito, 1, 'Dígito')}"
    )
def mask_telebradesco_login_text(value: object) -> str:
    """Mask the four-digit Telebradesco password inside a MEGABRA login.

    The wire login carries account identification, password and selector data in
    one fixed-width ASCII string.  Persisting that raw value in diagnostics or
    access history would expose the account password.  Keep the surrounding
    fields intact for troubleshooting while replacing only the password digits.
    """
    text = str(value or "")
    if text.upper().startswith("MEGABRA") and len(text) >= 23:
        return text[:19] + "****" + text[23:]
    return "<invalid-or-unavailable>"


def parse_telebradesco_login_text(value: object) -> Optional[dict]:
    """Parse MEGABRA credentials and the four-digit DDMM card selector."""
    value_text = str(value or "")
    match = re.fullmatch(
        rf"MEGABRA(\d{{4}})(\d{{7}})(\d)(\d{{4}})(\d{{{TELEBRADESCO_LOGIN_REMAINING_DIGITS}}})",
        value_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    agencia, conta, digito, senha, restante = match.groups()
    restante_prefixo = restante[:-TELEBRADESCO_DATA_CARTAO_DIGITS]
    data_cartao_raw = restante[-TELEBRADESCO_DATA_CARTAO_DIGITS:]
    data_cartao = _tele_normalize_card_identifier(data_cartao_raw)
    return {
        "agencia": agencia,
        "conta": conta,
        "digito": digito,
        "senha": senha,
        "data_cartao": data_cartao,
        "data_cartao_raw": data_cartao_raw,
        "restante_prefixo": restante_prefixo,
        "restante": restante,
        "key": telebradesco_account_key(agencia, conta, digito),
    }
def _default_telebradesco_account() -> dict:
    """Return an identity-only account template."""
    return {
        "nome": "",
        "agencia": "",
        "conta": "",
        "digito": "",
        "senha": "",
        "bloqueada": False,
    }
def normalize_telebradesco_account(value: object, *, fallback_content: Optional[dict] = None) -> dict:
    """Normalize account identity and operational metadata only.

    ``fallback_content`` is accepted for source compatibility but intentionally
    ignored. Financial data belongs exclusively to accounts_content.json.
    """
    del fallback_content
    if not isinstance(value, dict):
        raise ValueError("O registro da conta deve ser um objeto JSON.")
    raw = value
    required = ("agencia", "conta", "digito")
    missing = [key for key in required if raw.get(key) in (None, "")]
    if missing:
        raise ValueError("Campos obrigatórios ausentes: " + ", ".join(missing))

    account = _default_telebradesco_account()
    account["nome"] = str(raw.get("nome") or "CONTA TELEBRADESCO").strip()
    account["agencia"] = _tele_digits(raw.get("agencia"), 4, "Agência")
    account["conta"] = _tele_digits(raw.get("conta"), 7, "Conta corrente")
    account["digito"] = _tele_digits(raw.get("digito"), 1, "Dígito")
    raw_password = str(raw.get("senha") or "").strip()
    account["senha"] = _tele_digits(raw_password, 4, "Senha") if raw_password else ""
    blocked = raw.get("bloqueada", False)
    account["bloqueada"] = blocked if isinstance(blocked, bool) else str(blocked).strip().lower() in {"1", "true", "sim", "yes", "on"}
    try:
        account["total_accesses"] = int(raw.get("total_accesses") or raw.get("total_sessions") or 0)
    except (TypeError, ValueError):
        account["total_accesses"] = 0
    account["last_access_at"] = str(raw.get("last_access_at") or "")
    account["last_peer"] = str(raw.get("last_peer") or "")
    history = raw.get("access_history", [])
    account["access_history"] = copy.deepcopy(history[-50:]) if isinstance(history, list) else []
    return account
def default_telebradesco_accounts(legacy_content: Optional[dict] = None) -> dict:
    del legacy_content
    return {
        "schema": TELEBRADESCO_ACCOUNTS_SCHEMA,
        "updated_at": utc_now_iso(),
        "accounts": {},
    }
def default_telebradesco_accounts_content() -> dict:
    return {
        "schema": TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA,
        "updated_at": utc_now_iso(),
        "accounts": {},
    }
def _extract_embedded_telebradesco_contents(value: object) -> dict[str, dict]:
    extracted: dict[str, dict] = {}
    if not isinstance(value, dict) or not isinstance(value.get("accounts"), dict):
        return extracted
    for source_key, raw in value["accounts"].items():
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict):
            continue
        try:
            identity = normalize_telebradesco_account(raw)
            key = telebradesco_account_key(identity["agencia"], identity["conta"], identity["digito"])
            if str(source_key) != key:
                raise ValueError(f"chave JSON {source_key!r} não corresponde à conta {key}")
            extracted[key] = validate_telebradesco_content(raw["content"])
        except Exception as exc:
            raise TelebradescoAccountsError(f"Conteúdo embutido inválido em {source_key}: {exc}") from exc
    return extracted
def normalize_telebradesco_accounts(value: object, *, legacy_content: Optional[dict] = None) -> dict:
    del legacy_content
    if not isinstance(value, dict):
        raise TelebradescoAccountsError("A raiz do banco de contas deve ser um objeto JSON.")
    schema = str(value.get("schema") or TELEBRADESCO_ACCOUNTS_LEGACY_SCHEMA)
    if schema not in {TELEBRADESCO_ACCOUNTS_SCHEMA, TELEBRADESCO_ACCOUNTS_LEGACY_SCHEMA}:
        raise TelebradescoAccountsError(f"Schema de contas não suportado: {schema}")
    source = value.get("accounts")
    if not isinstance(source, dict):
        raise TelebradescoAccountsError("O campo accounts do banco deve ser um objeto JSON.")

    accounts: dict[str, dict] = {}
    errors: list[str] = []
    for source_key, entry in source.items():
        try:
            account = normalize_telebradesco_account(entry)
            key = telebradesco_account_key(account["agencia"], account["conta"], account["digito"])
            if str(source_key) != key:
                raise ValueError(f"chave JSON {source_key!r} não corresponde aos dados da conta ({key})")
            if key in accounts:
                raise ValueError(f"conta duplicada: {key}")
            accounts[key] = account
        except Exception as exc:
            errors.append(f"{source_key}: {exc}")
    if errors:
        raise TelebradescoAccountsError("Banco de contas inválido: " + " | ".join(errors))

    return {
        "schema": TELEBRADESCO_ACCOUNTS_SCHEMA,
        "updated_at": str(value.get("updated_at") or utc_now_iso()),
        "accounts": accounts,
    }
def normalize_telebradesco_accounts_content(
    value: object,
    *,
    account_keys: Optional[Iterable[str]] = None,
) -> dict:
    restrict_keys = account_keys is not None
    keys = [str(key) for key in (account_keys or [])]
    valid_keys = set(keys)
    if not isinstance(value, dict):
        raise TelebradescoAccountsError("A raiz do banco de conteúdo deve ser um objeto JSON.")

    schema = str(value.get("schema") or "")
    # Old legacy_content.json stored one content object without an account key.
    if schema.startswith("telebradesco_content_v") or any(
        key in value for key in ("conta_corrente", "poupanca", "investimentos_titulo", "investimentos", "cartoes", "cartao")
    ):
        if len(keys) != 1:
            raise TelebradescoAccountsError(
                "Conteúdo legado sem chave de conta só pode ser migrado quando existe exatamente uma conta."
            )
        return {
            "schema": TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA,
            "updated_at": utc_now_iso(),
            "accounts": {keys[0]: validate_telebradesco_content(value)},
        }

    if schema != TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA:
        raise TelebradescoAccountsError(f"Schema de conteúdo por conta não suportado: {schema or '<ausente>'}")
    source = value.get("accounts")
    if not isinstance(source, dict):
        raise TelebradescoAccountsError("O campo accounts do banco de conteúdo deve ser um objeto JSON.")

    contents: dict[str, dict] = {}
    errors: list[str] = []
    for source_key, raw_content in source.items():
        key = str(source_key)
        if restrict_keys and key not in valid_keys:
            logging.warning("TELE_CONTENT_ORPHAN_DROPPED account=%s", key)
            continue
        try:
            contents[key] = validate_telebradesco_content(raw_content)
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    if errors:
        raise TelebradescoAccountsError("Banco de conteúdo inválido: " + " | ".join(errors))
    return {
        "schema": TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA,
        "updated_at": str(value.get("updated_at") or utc_now_iso()),
        "accounts": contents,
    }
def _atomic_write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
def _restore_file_bytes(path: pathlib.Path, previous: Optional[bytes]) -> None:
    """Restore one file after a failed two-file Telebradesco transaction."""
    path = pathlib.Path(path)
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", suffix=".tmp", dir=str(path.parent))
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
def _merge_telebradesco_identity_and_content(identity_store: dict, content_store: dict) -> dict:
    merged = copy.deepcopy(identity_store)
    contents = content_store.get("accounts", {}) if isinstance(content_store, dict) else {}
    for key, account in merged.get("accounts", {}).items():
        if isinstance(account, dict):
            account["content"] = copy.deepcopy(
                contents.get(key) if isinstance(contents, dict) and isinstance(contents.get(key), dict)
                else blank_telebradesco_content()
            )
    return merged
def _split_telebradesco_merged_store(value: dict) -> tuple[dict, dict]:
    identity_store = normalize_telebradesco_accounts(value)
    raw_accounts = value.get("accounts", {}) if isinstance(value, dict) else {}
    contents: dict[str, dict] = {}
    for key in identity_store["accounts"]:
        raw_account = raw_accounts.get(key) if isinstance(raw_accounts, dict) else None
        raw_content = raw_account.get("content") if isinstance(raw_account, dict) else None
        contents[key] = validate_telebradesco_content(
            raw_content if isinstance(raw_content, dict) else blank_telebradesco_content()
        )
    content_store = {
        "schema": TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA,
        "updated_at": utc_now_iso(),
        "accounts": contents,
    }
    return identity_store, content_store
def write_telebradesco_accounts_content(path: pathlib.Path, value: dict, *, account_keys: Iterable[str]) -> dict:
    path = normalize_telebradesco_accounts_content_path(pathlib.Path(path))
    normalized = normalize_telebradesco_accounts_content(value, account_keys=account_keys)
    normalized["updated_at"] = utc_now_iso()
    _atomic_write_json(path, normalized)
    return normalized
def write_telebradesco_accounts(
    path: pathlib.Path,
    value: dict,
    *,
    accounts_content_file: Optional[pathlib.Path] = None,
) -> dict:
    path = pathlib.Path(path)
    content_path = normalize_telebradesco_accounts_content_path(
        pathlib.Path(accounts_content_file or path.with_name("accounts_content.json"))
    )
    with _TELE_ACCOUNTS_LOCK:
        identity_store, content_store = _split_telebradesco_merged_store(value)
        now = utc_now_iso()
        identity_store["updated_at"] = now

        content_changed = True
        if content_path.exists():
            try:
                existing_raw = json.loads(content_path.read_text(encoding="utf-8"))
                existing_content = normalize_telebradesco_accounts_content(
                    existing_raw,
                    account_keys=identity_store["accounts"].keys(),
                )
                if existing_content.get("accounts") == content_store.get("accounts"):
                    content_store = existing_content
                    content_changed = False
            except (OSError, UnicodeError, json.JSONDecodeError, TelebradescoAccountsError, ValueError, TypeError):
                content_changed = True

        previous_content = content_path.read_bytes() if content_changed and content_path.exists() else None
        if content_changed:
            content_store["updated_at"] = now
            _atomic_write_json(content_path, content_store)
        else:
            logging.debug("TELE_CONTENT_UNCHANGED path=%s action=skip-write", content_path)

        try:
            _atomic_write_json(path, identity_store)
        except (OSError, TypeError, ValueError):
            if content_changed:
                try:
                    _restore_file_bytes(content_path, previous_content)
                except OSError as rollback_exc:
                    logging.critical(
                        "TELE_CONTENT_ROLLBACK_FAILED path=%s error=%s",
                        content_path,
                        rollback_exc,
                    )
            raise
        return _merge_telebradesco_identity_and_content(identity_store, content_store)
def _read_telebradesco_content_source(
    content_path: pathlib.Path,
    *,
    account_keys: list[str],
) -> tuple[dict, bool]:
    if not content_path.exists():
        return default_telebradesco_accounts_content(), False
    raw = json.loads(content_path.read_text(encoding="utf-8"))
    normalized = normalize_telebradesco_accounts_content(raw, account_keys=account_keys)
    raw_accounts = raw.get("accounts", {}) if isinstance(raw, dict) else {}
    migrated = (
        str(raw.get("schema") or "") != TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA
        or any(
            not isinstance(raw_content, dict)
            or str(raw_content.get("schema") or "") != "telebradesco_content_v5"
            or "investimentos_titulo" in raw_content
            for raw_content in (raw_accounts.values() if isinstance(raw_accounts, dict) else ())
        )
    )
    return normalized, migrated
def read_telebradesco_accounts(
    path: pathlib.Path,
    *,
    accounts_content_file: Optional[pathlib.Path] = None,
    create_if_missing: bool = True,
) -> dict:
    path = pathlib.Path(path)
    content_path = pathlib.Path(accounts_content_file or path.with_name("accounts_content.json"))
    content_path = normalize_telebradesco_accounts_content_path(content_path)
    legacy_path = content_path.with_name("legacy_content.json")

    with _TELE_ACCOUNTS_LOCK:
        if not path.exists():
            identity_store = default_telebradesco_accounts()
            content_store = default_telebradesco_accounts_content()
            content_created = False
            if content_path.exists():
                try:
                    raw_content = json.loads(content_path.read_text(encoding="utf-8"))
                    content_store = normalize_telebradesco_accounts_content(raw_content)
                except (OSError, UnicodeError, json.JSONDecodeError, TelebradescoAccountsError, TypeError, ValueError) as exc:
                    backup = _backup_invalid_telebradesco_file(content_path)
                    raise TelebradescoAccountsError(
                        f"Banco de conteúdo Telebradesco inválido: {exc}",
                        path=content_path,
                        backup_path=backup,
                    ) from exc
                if content_store.get("accounts"):
                    raise TelebradescoAccountsError(
                        "accounts.json ausente enquanto accounts_content.json ainda possui dados financeiros; "
                        "o conteúdo foi preservado e a identidade deve ser restaurada antes da inicialização.",
                        path=path,
                    )
            if create_if_missing:
                if not content_path.exists():
                    _atomic_write_json(content_path, content_store)
                    content_created = True
                try:
                    _atomic_write_json(path, identity_store)
                except (OSError, TypeError, ValueError):
                    if content_created:
                        try:
                            _restore_file_bytes(content_path, None)
                        except OSError as rollback_exc:
                            logging.critical(
                                "TELE_CONTENT_ROLLBACK_FAILED path=%s error=%s",
                                content_path,
                                rollback_exc,
                            )
                    raise
                logging.info("TELE_ACCOUNTS_CREATED_EMPTY accounts=%s content=%s", path, content_path)
            return _merge_telebradesco_identity_and_content(identity_store, content_store)

        try:
            raw_accounts = json.loads(path.read_text(encoding="utf-8"))
            embedded_contents = _extract_embedded_telebradesco_contents(raw_accounts)
            identity_store = normalize_telebradesco_accounts(raw_accounts)
            account_keys = list(identity_store["accounts"].keys())

            content_store = default_telebradesco_accounts_content()
            content_store_migrated = False
            content_source = "new"
            if content_path.exists():
                try:
                    content_store, content_store_migrated = _read_telebradesco_content_source(
                        content_path,
                        account_keys=account_keys,
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, TelebradescoAccountsError, TypeError, ValueError) as exc:
                    backup = _backup_invalid_telebradesco_file(content_path)
                    raise TelebradescoAccountsError(
                        f"Banco de conteúdo Telebradesco inválido: {exc}",
                        path=content_path,
                        backup_path=backup,
                    ) from exc
            elif legacy_path.exists():
                raw_legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                if len(account_keys) == 1:
                    content_store = normalize_telebradesco_accounts_content(
                        raw_legacy,
                        account_keys=account_keys,
                    )
                    content_source = "legacy_content.json"
                elif embedded_contents:
                    # A global legacy file cannot be assigned safely to one of
                    # several accounts. Per-account embedded content is lossless.
                    content_store = default_telebradesco_accounts_content()
                    content_source = "embedded-account-content"
                    logging.warning(
                        "TELE_LEGACY_CONTENT_AMBIGUOUS path=%s account_count=%d "
                        "action=ignore-and-archive-after-split",
                        legacy_path,
                        len(account_keys),
                    )
                else:
                    raise TelebradescoAccountsError(
                        "legacy_content.json não possui chave de conta e não pode "
                        "ser associado automaticamente a múltiplas contas."
                    )
                content_store_migrated = True

            initial_content_keys = set(content_store.get("accounts", {}))
            contents = content_store.setdefault("accounts", {})
            for key in account_keys:
                if key in contents:
                    if key in embedded_contents and contents[key] != embedded_contents[key]:
                        if content_source == "legacy_content.json":
                            contents[key] = embedded_contents[key]
                            logging.warning(
                                "TELE_CONTENT_MIGRATION_CONFLICT account=%s action=prefer-embedded-account-content",
                                key,
                            )
                        else:
                            logging.warning(
                                "TELE_CONTENT_MIGRATION_CONFLICT account=%s action=keep-accounts_content",
                                key,
                            )
                    continue
                if key in embedded_contents:
                    contents[key] = embedded_contents[key]
                else:
                    contents[key] = blank_telebradesco_content()

            needs_accounts_rewrite = (
                str(raw_accounts.get("schema") or TELEBRADESCO_ACCOUNTS_LEGACY_SCHEMA)
                != TELEBRADESCO_ACCOUNTS_SCHEMA
                or bool(embedded_contents)
                or any(
                    isinstance(item, dict) and "content" in item
                    for item in (raw_accounts.get("accounts", {}) or {}).values()
                )
            )
            needs_content_rewrite = (
                not content_path.exists()
                or content_store_migrated
                or initial_content_keys != set(account_keys)
                or bool(embedded_contents)
            )

            if create_if_missing and (needs_accounts_rewrite or needs_content_rewrite):
                accounts_backup = _backup_telebradesco_migration_file(path, "content-split") if needs_accounts_rewrite else None
                content_backup = _backup_telebradesco_migration_file(content_path, "content-split") if content_path.exists() else None
                now = utc_now_iso()
                identity_store["updated_at"] = now
                content_store["schema"] = TELEBRADESCO_ACCOUNTS_CONTENT_SCHEMA
                content_store["updated_at"] = now
                previous_content = content_path.read_bytes() if content_path.exists() else None
                _atomic_write_json(content_path, content_store)
                try:
                    _atomic_write_json(path, identity_store)
                except (OSError, TypeError, ValueError):
                    try:
                        _restore_file_bytes(content_path, previous_content)
                    except OSError as rollback_exc:
                        logging.critical(
                            "TELE_CONTENT_ROLLBACK_FAILED path=%s error=%s",
                            content_path,
                            rollback_exc,
                        )
                    raise
                legacy_backup = None
                if legacy_path.exists() and legacy_path != content_path:
                    legacy_backup = _archive_legacy_telebradesco_content(legacy_path)
                logging.info(
                    "TELE_STORAGE_MIGRATED accounts=%s content=%s source=%s account_count=%d "
                    "embedded_count=%d accounts_backup=%s content_backup=%s legacy_backup=%s",
                    path,
                    content_path,
                    content_source,
                    len(account_keys),
                    len(embedded_contents),
                    accounts_backup,
                    content_backup,
                    legacy_backup,
                )

            return _merge_telebradesco_identity_and_content(identity_store, content_store)
        except TelebradescoAccountsError:
            raise
        except Exception as exc:
            backup = _backup_invalid_telebradesco_file(path)
            message = f"Banco de contas/conteúdo Telebradesco inválido: {exc}"
            logging.critical(
                "TELE_ACCOUNTS_UNAVAILABLE accounts=%s content=%s backup=%s error=%s",
                path,
                content_path,
                backup,
                exc,
            )
            raise TelebradescoAccountsError(message, path=path, backup_path=backup) from exc
def update_telebradesco_accounts(
    path: pathlib.Path,
    updater: Callable[[dict], _TeleUpdateResult],
    *,
    accounts_content_file: Optional[pathlib.Path] = None,
) -> tuple[dict, _TeleUpdateResult]:
    """Read, update and persist identity/content while holding one lock."""
    with _TELE_ACCOUNTS_LOCK:
        current = read_telebradesco_accounts(
            pathlib.Path(path),
            accounts_content_file=accounts_content_file,
            create_if_missing=True,
        )
        working = copy.deepcopy(current)
        result = updater(working)
        saved = write_telebradesco_accounts(
            pathlib.Path(path),
            working,
            accounts_content_file=accounts_content_file,
        )
        return saved, result
def _tele_form_first(form: dict[str, list[str]], key: str, default: str = "") -> str:
    """Return the first web-form value without depending on the admin package."""
    values = form.get(key, []) if isinstance(form, dict) else []
    if not isinstance(values, list) or not values:
        return default
    return str(values[0] if values[0] is not None else default).strip()


def telebradesco_account_from_form(form: dict[str, list[str]], content: dict) -> dict:
    account = normalize_telebradesco_account({
        "nome": _tele_form_first(form, "tele_account_name", "CONTA TELEBRADESCO"),
        "agencia": _tele_form_first(form, "tele_agencia"),
        "conta": _tele_form_first(form, "tele_conta"),
        "digito": _tele_form_first(form, "tele_digito"),
        "senha": _tele_form_first(form, "tele_senha"),
        "bloqueada": "tele_bloqueada" in form,
    })
    # In-memory merged view; write_telebradesco_accounts splits it again.
    account["content"] = validate_telebradesco_content(content)
    return account
def authenticate_telebradesco_login(accounts_data: dict, login_text: object) -> dict:
    credentials = parse_telebradesco_login_text(login_text)
    if not credentials:
        return {"status": "malformed_login", "status_code": "001", "credentials": None, "account": None}
    accounts = accounts_data.get("accounts", {}) if isinstance(accounts_data, dict) else {}
    account = accounts.get(credentials["key"]) if isinstance(accounts, dict) else None
    if not isinstance(account, dict):
        matching_account = None
        if isinstance(accounts, dict):
            for candidate in accounts.values():
                if not isinstance(candidate, dict):
                    continue
                if (
                    str(candidate.get("conta") or "") == credentials["conta"]
                    and str(candidate.get("digito") or "") == credentials["digito"]
                ):
                    matching_account = candidate
                    break
        if isinstance(matching_account, dict):
            return {
                "status": "invalid_agency",
                "status_code": "040",
                "credentials": credentials,
                "account": None,
                "matched_account": matching_account,
            }
        return {"status": "account_not_found", "status_code": "042", "credentials": credentials, "account": None}
    stored_password = str(account.get("senha", "") or "")
    if not stored_password:
        return {"status": "missing_password", "status_code": "010", "credentials": credentials, "account": account}
    if stored_password != credentials["senha"]:
        return {"status": "invalid_password", "status_code": "011", "credentials": credentials, "account": account}
    if bool(account.get("bloqueada", False)):
        return {"status": "blocked", "status_code": "020", "credentials": credentials, "account": account}
    return {"status": "ok", "status_code": "999", "credentials": credentials, "account": account}
def record_telebradesco_account_access(
    path: pathlib.Path,
    account_key: str,
    *,
    peer: str = "",
    login_text: str = "",
    accounts_content_file: Optional[pathlib.Path] = None,
) -> Optional[dict]:
    """Increment access counters without moving financial data into accounts.json."""
    key = str(account_key or "").strip()
    if not key:
        return None

    def _update(store: dict) -> Optional[dict]:
        accounts = store.setdefault("accounts", {})
        account = accounts.get(key) if isinstance(accounts, dict) else None
        if not isinstance(account, dict):
            return None
        now = utc_now_iso()
        try:
            current_total = int(account.get("total_accesses") or account.get("total_sessions") or 0)
        except (TypeError, ValueError):
            current_total = 0
        account["total_accesses"] = current_total + 1
        account["last_access_at"] = now
        account["last_peer"] = str(peer or "")
        access_history = account.setdefault("access_history", [])
        if not isinstance(access_history, list):
            access_history = []
            account["access_history"] = access_history
        access_history.append({
            "at": now,
            "peer": str(peer or ""),
            "login_text": mask_telebradesco_login_text(login_text),
        })
        account["access_history"] = access_history[-50:]
        accounts[key] = account
        return {"key": key, "total_accesses": account["total_accesses"], "last_access_at": now}

    try:
        _saved, result = update_telebradesco_accounts(
            path,
            _update,
            accounts_content_file=accounts_content_file,
        )
        return result if isinstance(result, dict) else None
    except Exception as exc:
        logging.warning("TELE_ACCESS_RECORD_FAILED key=%s peer=%s error=%s", key, peer, exc)
        return None
