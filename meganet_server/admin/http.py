"""Infraestrutura HTTP comum da interface administrativa."""

from __future__ import annotations

import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Optional

from .auth import ADMIN_WEB_SESSION_COOKIE, cookie_value, normalize_role


class AdminBaseHandler(BaseHTTPRequestHandler):
    """Base HTTP sem regras de negócio dos serviços emulados."""

    server_version = "ServidorMeganetTelebradescoAdmin/2.0.5"
    auth_manager = None

    def log_message(self, fmt: str, *vals: object) -> None:
        logging.info("ADMIN_WEB %s - " + fmt, self.address_string(), *vals)

    def _send(self, status: int, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("Content-Length inválido")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _redirect_with_cookie(self, location: str, cookie_header: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    def _current_admin_user(self) -> Optional[dict]:
        manager = self.auth_manager
        if manager is None:
            return None
        token = cookie_value(self.headers.get("Cookie", ""), ADMIN_WEB_SESSION_COOKIE)
        try:
            return manager.user_from_session_token(token)
        except (OSError, ValueError) as exc:
            logging.error("ADMIN_AUTH_REGISTRY_UNAVAILABLE error=%s", exc)
            return None

    def _is_admin_user(self, user: Optional[dict] = None) -> bool:
        user = user if isinstance(user, dict) else self._current_admin_user()
        return isinstance(user, dict) and normalize_role(user.get("role")) == "admin"
