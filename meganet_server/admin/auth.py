"""Autenticação e sessões da interface administrativa.

Mantém o formato ``admin_web_users.json`` e o cookie já usados pelo servidor.
A classe :class:`AdminAuthManager` concentra persistência e sessões em memória;
as funções auxiliares permanecem pequenas e independentes da camada HTTP.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import logging
import pathlib
import re
import secrets
import threading
import urllib.parse
from typing import Callable, Optional

from ..common import format_datetime
from ..storage import atomic_write_json

ADMIN_WEB_USERS_SCHEMA = "admin_web_users_v1"
ADMIN_WEB_SESSION_COOKIE = "SMT_ADMIN_SESSION"
_ADMIN_WEB_CURRENT_USER = threading.local()
_ADMIN_USERS_LOCK = threading.RLock()


def set_current_user(user: Optional[dict]) -> None:
    _ADMIN_WEB_CURRENT_USER.user = user if isinstance(user, dict) else None


def get_current_user() -> Optional[dict]:
    user = getattr(_ADMIN_WEB_CURRENT_USER, "user", None)
    return user if isinstance(user, dict) else None


def password_hash(password: str) -> str:
    iterations = 180000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def password_verify(password: str, stored_hash: object) -> bool:
    text = str(stored_hash or "")
    try:
        scheme, iterations, salt_b64, digest_b64 = text.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def normalize_role(value: object) -> str:
    return "admin" if str(value or "").strip().lower() == "admin" else "user"


def role_label(role: object) -> str:
    return "Administrador" if normalize_role(role) == "admin" else "Usuário somente leitura"


def initials(username: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(username or "")).strip()
    if not text:
        return "?"
    parts = text.split()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()



def cookie_value(cookie_header: str, name: str) -> str:
    for item in str(cookie_header or "").split(";"):
        key, sep, value = item.strip().partition("=")
        if sep and key == name:
            return urllib.parse.unquote(value)
    return ""


class AdminAuthManager:
    """Persistent administrator registry plus process-local web sessions."""

    def __init__(self, users_path: pathlib.Path, *, utc_now: Callable[[], str]) -> None:
        self.users_path = pathlib.Path(users_path)
        self.utc_now = utc_now
        self.sessions: dict[str, dict] = {}

    def read_users(self) -> dict:
        with _ADMIN_USERS_LOCK:
            try:
                data = json.loads(self.users_path.read_text(encoding="utf-8")) if self.users_path.exists() else {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            users = data.get("users") if isinstance(data.get("users"), dict) else {}
            data["schema"] = ADMIN_WEB_USERS_SCHEMA
            data["users"] = users
            data.setdefault("created_at", self.utc_now())
            data.setdefault("updated_at", self.utc_now())
            return data

    def write_users(self, data: dict) -> None:
        with _ADMIN_USERS_LOCK:
            data = data if isinstance(data, dict) else {}
            data["schema"] = ADMIN_WEB_USERS_SCHEMA
            data["updated_at"] = self.utc_now()
            atomic_write_json(
                self.users_path,
                data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )

    def ensure_users(self) -> dict:
        with _ADMIN_USERS_LOCK:
            if self.users_path.exists():
                try:
                    existing = json.loads(self.users_path.read_text(encoding="utf-8"))
                except OSError:
                    raise
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid admin users registry: {self.users_path}") from exc
                if not isinstance(existing, dict) or ("users" in existing and not isinstance(existing.get("users"), dict)):
                    raise ValueError(f"invalid admin users registry: {self.users_path}")
            data = self.read_users()
            users = data.setdefault("users", {})
            changed = False
            if not users:
                now = self.utc_now()
                users["admin"] = {
                    "username": "admin",
                    "role": "admin",
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                    "password_hash": password_hash("admin"),
                }
                changed = True
                logging.warning("ADMIN_WEB_DEFAULT_USER_CREATED path=%s username=admin password=admin", self.users_path)
            if changed:
                self.write_users(data)
            return data

    @contextmanager
    def locked_users(self):
        """Hold the process-wide users lock across one read/modify/write transaction."""
        with _ADMIN_USERS_LOCK:
            yield self.ensure_users()

    def user_from_session_token(self, token: str) -> Optional[dict]:
        if not token:
            return None
        session = self.sessions.get(token)
        if not isinstance(session, dict):
            return None
        username = str(session.get("username") or "")
        data = self.ensure_users()
        rec = data.get("users", {}).get(username)
        if not isinstance(rec, dict) or not bool(rec.get("active", True)):
            self.sessions.pop(token, None)
            return None
        session["last_seen_at"] = self.utc_now()
        return {"username": username, "role": normalize_role(rec.get("role")), "active": True}

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.utc_now()
        self.sessions[token] = {"username": str(username), "created_at": now, "last_seen_at": now}
        return token

    def destroy_session(self, token: str) -> None:
        if token:
            self.sessions.pop(token, None)

