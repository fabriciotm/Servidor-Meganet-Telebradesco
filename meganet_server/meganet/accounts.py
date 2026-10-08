"""MegaNet cadastro, identity, access-code and password-change persistence.
"""
from __future__ import annotations

import json
import logging
import pathlib
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..common import utc_now_iso
from ..storage import atomic_write_json
from .protocol import DecodedFrame, ascii_text, display_payload

MEGANET_ROOT_DIR = pathlib.Path("meganet")

DEFAULT_CADASTRO_DB = MEGANET_ROOT_DIR / "cadastros.json"

DEFAULT_SESSION_OUTPUT_DIR = MEGANET_ROOT_DIR / "sessions"

ACCESS_CODE_TABLE = "0123456789ABCDEF"

ACCESS_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_CADASTRO_WRITE_LOCK = threading.RLock()
_PASSWORD_CHANGE_WRITE_LOCK = threading.RLock()

def normalize_access_code_base(base: str) -> str:
    base = "".join(ch for ch in str(base or "").strip().upper() if not ch.isspace())
    if len(base) != 10:
        raise ValueError("A base deve ter exatamente 10 caracteres.")
    return base

def calculate_access_code_suffix(base10: str) -> str:
    base10 = normalize_access_code_base(base10)
    total = sum(ord(ch) for ch in base10)
    return ACCESS_CODE_TABLE[(total >> 4) & 0x0F] + ACCESS_CODE_TABLE[total & 0x0F]

def generate_access_code(base10: str) -> str:
    base10 = normalize_access_code_base(base10)
    return base10 + calculate_access_code_suffix(base10)

def validate_access_code(codigo: str) -> bool:
    codigo = "".join(ch for ch in str(codigo or "").strip().upper() if not ch.isspace())
    if len(codigo) < 12:
        return False
    try:
        return codigo[10:12] == calculate_access_code_suffix(codigo[:10])
    except ValueError:
        return False

def generate_random_access_code(alphabet: str = ACCESS_CODE_ALPHABET) -> str:
    alphabet = "".join(ch for ch in str(alphabet or ACCESS_CODE_ALPHABET).upper() if not ch.isspace()) or ACCESS_CODE_ALPHABET
    base = "".join(random.choice(alphabet) for _ in range(10))
    return generate_access_code(base)

FIELD_NAMES = [
    "nome",
    "apelido",
    "data_nascimento",
    "sexo",
    "nome_meganet",
    "senha",
    "codigo_acesso",
    "fone_meganet",
    "endereco",
    "numero",
    "bairro",
    "cidade",
    "estado",
    "cep",
    "telefone_casa",
    "telefone_trabalho_pai_mae",
    "telefone_trabalho_e_de_pai_ou_mae",
    "pai",
    "mae",
    "irmao1_nome",
    "irmao1_nascimento",
    "irmao1_sexo",
    "irmao2_nome",
    "irmao2_nascimento",
    "irmao2_sexo",
    "irmao3_nome",
    "irmao3_nascimento",
    "irmao3_sexo",
    "videogame_tectoy",
    "outros_videogames",
    "quantidade_cartuchos",
]

def normalize_user_key(value: str) -> str:
    """Create a stable, filesystem/JSON friendly key for a cadastro user."""
    value = value.strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_.@-]+", "", value)
    return value.strip("._-")

def normalize_codigo_acesso(value: object) -> str:
    """Normalize codigo_acesso for duplicate detection.

    Access codes are treated as identifiers, not display text: leading/trailing
    spaces and accidental internal spaces are ignored, and comparison is
    case-insensitive.
    """
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text.upper()

def normalize_mail_domain(domain: object) -> str:
    text = str(domain or "meganet.tectoy.com.br").strip().lower()
    return text or "meganet.tectoy.com.br"

def meganet_email_for_name(nome_meganet: object, domain: object = "meganet.tectoy.com.br") -> str:
    local = normalize_user_key(str(nome_meganet or ""))
    if not local:
        local = "usuario"
    return f"{local}@{normalize_mail_domain(domain)}"

def normalize_email_address(value: object, domain: object = "meganet.tectoy.com.br") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "@" not in text:
        text = f"{normalize_user_key(text)}@{normalize_mail_domain(domain)}"
    local, _, dom = text.partition("@")
    return f"{normalize_user_key(local)}@{normalize_mail_domain(dom)}"

def email_local_part(address: object) -> str:
    text = str(address or "").strip().lower()
    local = text.split("@", 1)[0]
    return normalize_user_key(local)

def choose_user_key(record: dict, profile_name: str, fallback: str) -> tuple[str, str]:
    """Return (key, source_field) for the persistent cadastro DB."""
    candidates = [
        ("nome_meganet", record.get("nome_meganet", "")),
        ("profile_name", profile_name),
        ("nome", record.get("nome", "")),
        ("apelido", record.get("apelido", "")),
    ]
    for source, value in candidates:
        if not value:
            continue
        key = normalize_user_key(str(value))
        if key:
            return key, source
    return normalize_user_key(fallback) or fallback, "session_id"

@dataclass
class AuthResult:
    allowed: bool
    known_user: bool
    user_key: Optional[str]
    reason: str
    entry: Optional[dict] = None

@dataclass
class CadastroState:
    fields: list[bytes] = field(default_factory=list)
    in_cadastro: bool = False
    waiting_item_value: bool = False
    packet_log: list[dict] = field(default_factory=list)

    def add_packet(self, frame: DecodedFrame) -> None:
        self.packet_log.append({
            "seq": frame.seq,
            "len": len(frame.payload),
            "valid": frame.valid,
            "payload_text": display_payload(frame.payload),
            "payload_hex": frame.payload.hex(" "),
        })

    def maybe_add_field(self, payload: bytes) -> Optional[tuple[int, bytes]]:
        token = payload.rstrip(b"\r")
        if token in {
            b"#get cadastro#",
            b"#begin cadastro#",
            b"#item#",
            b"#end cadastro#",
            b"#bye#",
        }:
            return None
        if self.in_cadastro:
            self.fields.append(payload.rstrip(b"\r"))
            return len(self.fields), self.fields[-1]
        return None

    def as_record(self) -> dict:
        record = {}
        for idx, raw in enumerate(self.fields, start=1):
            name = FIELD_NAMES[idx - 1] if idx <= len(FIELD_NAMES) else f"campo_{idx:02d}"
            record[name] = ascii_text(raw)
        return record

class CadastroDatabase:
    """Small JSON-backed cadastro registry.

    The file intentionally stays human-readable.  Password-like fields are kept
    because this is an emulator/reverse-engineering prototype, but the file
    should be treated as sensitive in real use.
    """

    SCHEMA = "meganet_cadastros_v1"

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.data = self._load()
        self._mtime_ns = self._stat_mtime_ns()

    def _stat_mtime_ns(self) -> int:
        try:
            return int(self.path.stat().st_mtime_ns)
        except OSError:
            return 0

    def _refresh_if_changed(self) -> bool:
        """Reload the cadastro when another session/admin request changed it.

        Each MegaNet connection owns a CadastroDatabase instance.  Previously
        those instances cached the JSON until the process restarted, so a user
        created/edited by another session could remain invisible to mail routing.
        """
        current = self._stat_mtime_ns()
        if current == self._mtime_ns:
            return False
        previous = self._mtime_ns
        self.data = self._load()
        self._mtime_ns = current
        logging.info(
            "CADASTRO_RUNTIME_RELOAD path=%s old_mtime_ns=%d new_mtime_ns=%d users=%d",
            self.path, previous, current, len(self.data.get("users", {})) if isinstance(self.data, dict) else 0,
        )
        return True

    def _empty(self) -> dict:
        now = utc_now_iso()
        return {
            "schema": self.SCHEMA,
            "created_at": now,
            "updated_at": now,
            "users": {},
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logging.warning("Could not read cadastro DB %s: %s; starting empty", self.path, exc)
            return self._empty()
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            logging.warning("Invalid cadastro DB structure in %s; starting empty", self.path)
            return self._empty()
        data.setdefault("schema", self.SCHEMA)
        data.setdefault("created_at", utc_now_iso())
        data.setdefault("updated_at", utc_now_iso())
        return data

    def _atomic_write(self) -> None:
        with _CADASTRO_WRITE_LOCK:
            atomic_write_json(self.path, self.data, ensure_ascii=False, indent=2, sort_keys=True)
            self._mtime_ns = self._stat_mtime_ns()

    def _reload_for_update(self) -> None:
        """Reload the latest persisted cadastro while holding the write lock."""
        self.data = self._load()
        self._mtime_ns = self._stat_mtime_ns()

    def find_returning_user(self, profile_name: str) -> Optional[tuple[str, dict]]:
        self._refresh_if_changed()
        if not profile_name:
            return None
        profile_key = normalize_user_key(profile_name)
        users = self.data.get("users", {})
        if profile_key in users:
            return profile_key, users[profile_key]
        for key, entry in users.items():
            fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
            values = [
                entry.get("profile_name", ""),
                fields.get("nome_meganet", ""),
                fields.get("nome", ""),
                fields.get("apelido", ""),
            ]
            if any(normalize_user_key(str(v)) == profile_key for v in values if v):
                return key, entry
        return None

    @staticmethod
    def _same_secret(left: object, right: object) -> bool:
        # Keep password comparison case-sensitive, but ignore accidental CR/LF/spaces.
        return str(left or "").strip() == str(right or "").strip()

    @staticmethod
    def _same_meganet_secret(left: object, right: object, password_case: str) -> bool:
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        if password_case == "exact":
            return left_text == right_text
        if password_case == "upper":
            return left_text.upper() == right_text.upper()
        if password_case == "lower":
            return left_text.lower() == right_text.lower()
        # Default: case-insensitive. This matches the observed ROM behavior where
        # a typed password such as 123abc is transmitted in #id as 123ABC.
        return left_text.casefold() == right_text.casefold()

    def validate_user(
        self,
        *,
        profile_name: str,
        bootstrap_login: str,
        bootstrap_password: str,
        auth_mode: str,
        auth_password_source: str,
        auth_check_bootstrap_login: bool,
    ) -> AuthResult:
        """Validate a returning user against the persistent cadastro DB.

        auth_mode:
          off           -> always allow; only mark whether the user was known.
          allow-new     -> validate known users, but allow an unknown user to register.
          require-known -> reject unknown users.

        auth_password_source:
          off            -> only existence/profile check.
          bootstrap      -> compare against the saved bootstrap_password.
          cadastro-senha -> compare against fields.senha.
          either         -> accept bootstrap_password or fields.senha.
        """
        found = self.find_returning_user(profile_name)

        if auth_mode == "off":
            if found:
                return AuthResult(True, True, found[0], "auth_off_known_user", found[1])
            return AuthResult(True, False, None, "auth_off_unknown_user", None)

        if not found:
            if auth_mode == "allow-new":
                return AuthResult(True, False, None, "new_user_allowed_for_registration", None)
            return AuthResult(False, False, None, "unknown_user", None)

        user_key, entry = found
        fields = entry.get("fields", {}) if isinstance(entry, dict) else {}

        if auth_check_bootstrap_login:
            stored_login = str(entry.get("bootstrap_login") or "").strip().lower()
            current_login = str(bootstrap_login or "").strip().lower()
            if stored_login and stored_login != current_login:
                return AuthResult(False, True, user_key, "bootstrap_login_mismatch", entry)

        if auth_password_source == "off":
            return AuthResult(True, True, user_key, "known_user_no_password_check", entry)

        candidates: list[tuple[str, object]] = []
        if auth_password_source in {"bootstrap", "either"}:
            candidates.append(("bootstrap_password", entry.get("bootstrap_password")))
        if auth_password_source in {"cadastro-senha", "either"}:
            candidates.append(("fields.senha", fields.get("senha")))

        for source, stored_secret in candidates:
            if stored_secret and self._same_secret(bootstrap_password, stored_secret):
                return AuthResult(True, True, user_key, f"password_ok:{source}", entry)

        checked = ",".join(source for source, _ in candidates) or "none"
        return AuthResult(False, True, user_key, f"password_mismatch:{checked}", entry)

    def find_user_by_meganet_name(self, nome_meganet: str) -> Optional[tuple[str, dict]]:
        """Find an existing cadastro entry by fields.nome_meganet.

        This is the real Mega Net account identifier captured in the cadastro
        screen: "Escolha seu nome no MEGA NET". It is intentionally separate
        from the PPP/bootstrap profile field such as MAURICIO.
        """
        self._refresh_if_changed()
        key = normalize_user_key(nome_meganet)
        if not key:
            return None
        users = self.data.get("users", {})
        # Fast path: /normally keys the user by nome_meganet.
        # strictness: for MegaNet login, fields.nome_meganet is the ONLY
        # accepted account identifier.  The JSON dictionary key is considered
        # an internal storage key only and is never a valid login fallback.
        # This prevents users["kavera"] from authenticating #id kavera if
        # fields.nome_meganet was edited to a different value.
        if key in users and isinstance(users[key], dict):
            entry = users[key]
            fields = entry.get("fields", {}) if isinstance(entry.get("fields"), dict) else {}
            stored_nome_meganet = str(fields.get("nome_meganet", "") or "").strip()
            if normalize_user_key(stored_nome_meganet) != key:
                logging.info(
                    "CADASTRO_LOOKUP_KEY_IGNORED requested=%s json_key=%s fields.nome_meganet=%s action=field_only_login",
                    nome_meganet, key, stored_nome_meganet or "<empty>",
                )
        for user_key, entry in users.items():
            if not isinstance(entry, dict):
                continue
            fields = entry.get("fields", {}) if isinstance(entry.get("fields"), dict) else {}
            stored_nome_meganet = fields.get("nome_meganet", "")
            if normalize_user_key(str(stored_nome_meganet)) == key:
                logging.info(
                    "CADASTRO_LOOKUP_FIELD_MATCH requested=%s json_key=%s fields.nome_meganet=%s action=multi_user_field_login",
                    nome_meganet, user_key, stored_nome_meganet,
                )
                return user_key, entry
        logging.info(
            "CADASTRO_LOOKUP_FIELD_NOT_FOUND requested=%s action=multi_user_field_login",
            nome_meganet,
        )
        return None

    def find_user_by_codigo_acesso(self, codigo_acesso: object, *, exclude_user_key: Optional[str] = None) -> Optional[tuple[str, dict]]:
        """Find an existing cadastro entry by fields.codigo_acesso.

        Used only during cadastro/new-user registration.  Login is never
        performed by codigo_acesso; login remains fields.nome_meganet +
        fields.senha.
        """
        self._refresh_if_changed()
        normalized = normalize_codigo_acesso(codigo_acesso)
        if not normalized:
            return None
        users = self.data.get("users", {})
        for user_key, entry in users.items():
            if exclude_user_key and str(user_key) == str(exclude_user_key):
                continue
            if not isinstance(entry, dict):
                continue
            fields = entry.get("fields", {}) if isinstance(entry.get("fields"), dict) else {}
            stored_codigo = fields.get("codigo_acesso", "")
            if normalize_codigo_acesso(stored_codigo) == normalized:
                logging.info(
                    "CADASTRO_LOOKUP_CODIGO_ACESSO_MATCH requested=%s json_key=%s fields.codigo_acesso=%s action=duplicate_codigo_acesso_check",
                    codigo_acesso, user_key, stored_codigo,
                )
                return user_key, entry
        logging.info(
            "CADASTRO_LOOKUP_CODIGO_ACESSO_NOT_FOUND requested=%s action=duplicate_codigo_acesso_check",
            codigo_acesso,
        )
        return None

    def find_user_by_meganet_email(self, email: object, mail_domain: str = "meganet.tectoy.com.br") -> Optional[tuple[str, dict]]:
        """Find a user by fields.email_meganet or by nome_meganet-derived address."""
        self._refresh_if_changed()
        normalized = normalize_email_address(email, mail_domain)
        if not normalized:
            return None
        users = self.data.get("users", {})
        for user_key, entry in users.items():
            if not isinstance(entry, dict):
                continue
            fields = entry.get("fields", {}) if isinstance(entry.get("fields"), dict) else {}
            stored_email = normalize_email_address(fields.get("email_meganet") or fields.get("email") or "", mail_domain)
            stored_nome_email = normalize_email_address(meganet_email_for_name(fields.get("nome_meganet", ""), mail_domain), mail_domain)
            if normalized and normalized in {stored_email, stored_nome_email}:
                logging.info(
                    "CADASTRO_LOOKUP_EMAIL_MATCH requested=%s json_key=%s email=%s action=internal_mail_delivery",
                    normalized, user_key, stored_email or stored_nome_email,
                )
                return user_key, entry
        logging.info(
            "CADASTRO_LOOKUP_EMAIL_NOT_FOUND requested=%s action=internal_mail_delivery",
            normalized,
        )
        return None

    def email_for_nome_meganet(self, nome_meganet: object, mail_domain: str = "meganet.tectoy.com.br") -> str:
        found = self.find_user_by_meganet_name(str(nome_meganet or ""))
        if found:
            fields = found[1].get("fields", {}) if isinstance(found[1], dict) else {}
            return normalize_email_address(fields.get("email_meganet") or meganet_email_for_name(fields.get("nome_meganet"), mail_domain), mail_domain)
        return normalize_email_address(meganet_email_for_name(nome_meganet, mail_domain), mail_domain)


    def validate_meganet_credentials(self, fields: dict, auth_mode: str, password_case: str = "case-insensitive") -> AuthResult:
        """Validate the Mega Net account using cadastro fields.nome_meganet/senha.

        auth_mode:
          off           -> always allow and do not check the cadastro DB.
          allow-new     -> allow first registration; validate password on known users.
          require-known -> reject if fields.nome_meganet is not already in the DB.
        """
        nome_meganet = str(fields.get("nome_meganet") or "").strip()
        senha = str(fields.get("senha") or "").strip()

        if auth_mode == "off":
            found = self.find_user_by_meganet_name(nome_meganet) if nome_meganet else None
            if found:
                return AuthResult(True, True, found[0], "meganet_auth_off_known_user", found[1])
            return AuthResult(True, False, None, "meganet_auth_off", None)

        if not nome_meganet:
            return AuthResult(False, False, None, "missing_nome_meganet", None)
        if not senha:
            return AuthResult(False, False, None, "missing_senha", None)

        found = self.find_user_by_meganet_name(nome_meganet)
        if not found:
            if auth_mode == "allow-new":
                return AuthResult(True, False, None, "new_meganet_user_allowed_for_registration", None)
            return AuthResult(False, False, None, "unknown_nome_meganet", None)

        user_key, entry = found
        stored_fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
        stored_senha = stored_fields.get("senha")
        if self._same_meganet_secret(senha, stored_senha, password_case):
            return AuthResult(True, True, user_key, f"meganet_password_ok:fields.senha:{password_case}", entry)
        return AuthResult(False, True, user_key, f"meganet_password_mismatch:fields.senha:{password_case}", entry)

    def validate_meganet_id_credentials(
        self,
        *,
        nome_meganet: str,
        senha: str,
        auth_mode: str,
        password_case: str,
    ) -> AuthResult:
        """Validate the actual ROM #id user/password packet against the cadastro DB."""
        fields = {"nome_meganet": nome_meganet, "senha": senha}
        return self.validate_meganet_credentials(fields, auth_mode=auth_mode, password_case=password_case)


    def record_user_access(self, *, user_key: object = None, nome_meganet: object = "", session_id: str = "", peer: str = "", source: str = "meganet-login") -> Optional[tuple[str, pathlib.Path]]:
        """Increment the persisted MegaNet access counter after a successful login.

        Cadastro creation already records the first registration session.  Returning
        users authenticate through #id without re-running cadastro, so their
        total_sessions/history must be updated explicitly here.
        """
        with _CADASTRO_WRITE_LOCK:
            self._reload_for_update()
            found: Optional[tuple[str, dict]] = None
            users = self.data.setdefault("users", {})
            key_text = str(user_key or "").strip()
            if key_text and isinstance(users.get(key_text), dict):
                found = (key_text, users[key_text])
            if found is None and str(nome_meganet or "").strip():
                found = self.find_user_by_meganet_name(str(nome_meganet or ""))
            if found is None:
                return None
            key, entry = found
            if not isinstance(entry, dict):
                return None
            now = utc_now_iso()
            try:
                current_total = int(entry.get("total_sessions") or 0)
            except (TypeError, ValueError):
                current_total = 0
            entry["total_sessions"] = current_total + 1
            entry["last_session_id"] = str(session_id or "")
            entry["last_peer"] = str(peer or "")
            entry["updated_at"] = now
            history = entry.setdefault("history", [])
            if not isinstance(history, list):
                history = []
                entry["history"] = history
            history.append({
                "session_id": str(session_id or ""),
                "peer": str(peer or ""),
                "at": now,
                "source": str(source or "meganet-login"),
            })
            entry["history"] = history[-50:]
            users[key] = entry
            self.data["updated_at"] = now
            self._atomic_write()
            return key, self.path

    def update_meganet_password(self, nome_meganet: str, nova_senha: str, *, session_id: str = "", peer: str = "", source: str = "password-change-probe") -> Optional[tuple[str, pathlib.Path]]:
        """Update fields.senha for an existing MegaNet user.

        This is deliberately used only when --password-change-apply is set.
        The diagnostic probe records candidates without changing the cadastro DB.
        """
        with _CADASTRO_WRITE_LOCK:
            self._reload_for_update()
            found = self.find_user_by_meganet_name(nome_meganet)
            if not found:
                return None
            user_key, entry = found
            users = self.data.setdefault("users", {})
            if not isinstance(entry, dict):
                return None
            fields = entry.setdefault("fields", {})
            if not isinstance(fields, dict):
                fields = {}
                entry["fields"] = fields
            old_senha = fields.get("senha")
            now = utc_now_iso()
            fields["senha"] = nova_senha
            history = entry.setdefault("password_history", [])
            if not isinstance(history, list):
                history = []
                entry["password_history"] = history
            history.append({
                "timestamp": now,
                "old_senha": old_senha,
                "new_senha": nova_senha,
                "session_id": session_id,
                "peer": peer,
                "source": source,
            })
            entry["updated_at"] = now
            users[user_key] = entry
            self.data["updated_at"] = now
            self._atomic_write()
            return user_key, self.path

    def upsert_registration(
        self,
        *,
        session_id: str,
        peer: str,
        profile_name: str,
        bootstrap_login: str,
        bootstrap_password: str,
        fields: dict,
        packet_count: int,
        mail_domain: str = "meganet.tectoy.com.br",
    ) -> tuple[str, pathlib.Path]:
        with _CADASTRO_WRITE_LOCK:
            self._reload_for_update()
            now = utc_now_iso()
            fields = dict(fields or {})
            nome_for_email = fields.get("nome_meganet") or profile_name
            fields["email_meganet"] = normalize_email_address(
                fields.get("email_meganet") or meganet_email_for_name(nome_for_email, mail_domain),
                mail_domain,
            )
            user_key, key_source = choose_user_key(fields, profile_name, session_id)
            users = self.data.setdefault("users", {})
            existing = users.get(user_key, {}) if isinstance(users.get(user_key), dict) else {}
            created_at = existing.get("created_at") or now
            try:
                previous_sessions = int(existing.get("total_sessions") or 0)
            except (TypeError, ValueError):
                previous_sessions = 0
            total_sessions = previous_sessions + 1
            history = existing.get("history", [])
            if not isinstance(history, list):
                history = []
            history.append({
                "session_id": session_id,
                "peer": peer,
                "at": now,
                "packet_count": packet_count,
            })
            history = history[-20:]
            users[user_key] = {
                "user_key": user_key,
                "key_source": key_source,
                "created_at": created_at,
                "updated_at": now,
                "total_sessions": total_sessions,
                "last_session_id": session_id,
                "last_peer": peer,
                "profile_name": profile_name,
                "bootstrap_login": bootstrap_login,
                "bootstrap_password": bootstrap_password,
                "fields": fields,
                "history": history,
            }
            self.data["updated_at"] = now
            self._atomic_write()
            return user_key, self.path

def _safe_ascii_text(value: object) -> str:
    """Return a MegaNet-safe ASCII string without protocol delimiters."""
    text = str(value or "")
    text = text.replace("#", "")
    # The ROM/server path is ASCII-oriented. Keep accents out of persisted outbound records.
    repl = {
        "á":"a", "à":"a", "â":"a", "ã":"a", "ä":"a", "Á":"A", "À":"A", "Â":"A", "Ã":"A", "Ä":"A",
        "é":"e", "ê":"e", "ë":"e", "É":"E", "Ê":"E", "Ë":"E",
        "í":"i", "ï":"i", "Í":"I", "Ï":"I",
        "ó":"o", "ô":"o", "õ":"o", "ö":"o", "Ó":"O", "Ô":"O", "Õ":"O", "Ö":"O",
        "ú":"u", "ü":"u", "Ú":"U", "Ü":"U",
        "ç":"c", "Ç":"C",
    }
    for a, b in repl.items():
        text = text.replace(a, b)
    return text.encode("ascii", "replace").decode("ascii").strip()

class PasswordChangeStore:
    """Persistent capture store for TROCA DE SENHA reverse-engineering.

    The password-change flow records packets seen during --service troca-senha and can
    apply the real ROM token #senha <usuario> <senha_antiga> <senha_nova>#
    when --password-change-apply is enabled.
    """

    def __init__(self, base_dir: pathlib.Path) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.base_dir / "password_change_probe.json"
        self.csv_path = self.base_dir / "password_change_probe.csv"
        if not self.events_path.exists():
            self._write({"events": []})

    def _read(self) -> dict:
        try:
            data = json.loads(self.events_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("events", [])
                return data
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        return {"events": []}

    def _write(self, data: dict) -> None:
        atomic_write_json(self.events_path, data, ensure_ascii=False, indent=2)
        try:
            self._write_csv(data.get("events", []))
        except (OSError, UnicodeError) as exc:
            # The JSON event log is the primary capture store. CSV is a
            # derived/operator-friendly view and must not invalidate a
            # successfully persisted password-change event.
            logging.warning(
                "PASSWORD_CHANGE_CSV_WRITE_FAILED path=%s error=%s",
                self.csv_path, exc,
            )

    def append(self, entry: dict) -> pathlib.Path:
        with _PASSWORD_CHANGE_WRITE_LOCK:
            data = self._read()
            events = data.setdefault("events", [])
            if not isinstance(events, list):
                events = []
                data["events"] = events
            events.append(entry)
            self._write(data)
            return self.events_path

    def _write_csv(self, events: list[dict]) -> None:
        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
            f.write('timestamp,session_id,peer,kind,user,token,payload_text,note\n')
            for e in events:
                def cell(v: object) -> str:
                    t = str(v or "").replace('"', '""').replace("\r", " ").replace("\n", " ")
                    return f'"{t}"'
                f.write(",".join([
                    cell(e.get("timestamp")),
                    cell(e.get("session_id")),
                    cell(e.get("peer")),
                    cell(e.get("kind")),
                    cell(e.get("user")),
                    cell(e.get("token")),
                    cell(e.get("payload_text")),
                    cell(e.get("note")),
                ]) + "\n")

