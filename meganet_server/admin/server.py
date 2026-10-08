#!/usr/bin/env python3
"""Administrative HTTP server composition and lifecycle."""

from __future__ import annotations

import argparse
import logging
import pathlib
import secrets
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Optional

from .auth import (
    ADMIN_WEB_SESSION_COOKIE, AdminAuthManager, cookie_value as _get_cookie_value,
    normalize_role as _normalize_admin_role, password_verify as _admin_password_verify,
    set_current_user as _set_admin_web_current_user,
)
from .controllers.auth_system import AuthSystemControllerMixin
from .controllers.meganet import MegaNetControllerMixin
from .controllers.telebradesco import TelebradescoControllerMixin
from .controllers.dashboard import DashboardControllerMixin
from .controllers.settings import SettingsControllerMixin
from .helpers import _request_close_meganet_sessions
from .http import AdminBaseHandler
from .page import _ADMIN_CSRF_TOKEN, _SIDE_BRAND_LOGO_PNG, _inject_admin_csrf, html_page
from .router import (
    ADMIN_GET_PRIVATE_ROUTES, ADMIN_GET_PUBLIC_ROUTES, ADMIN_POST_PRIVATE_ROUTES,
    validate_route_table,
)
from .views import (
    render_admin_user_management, render_login_page, user_create_form, user_json_form,
)
from ..common import utc_now_iso
from ..config import normalize_bind_host as _normalize_bind_host

@dataclass
class AdminApplicationContext:
    """Dependencies owned by one administrative HTTP server instance."""

    args: argparse.Namespace
    config_path: pathlib.Path
    auth_manager: AdminAuthManager

    @classmethod
    def create(cls, args: argparse.Namespace) -> "AdminApplicationContext":
        config_path = pathlib.Path(getattr(args, "config", "meganet_config.json"))
        auth_manager = AdminAuthManager(config_path.parent / "admin_web_users.json", utc_now=utc_now_iso)
        auth_manager.ensure_users()
        return cls(args=args, config_path=config_path, auth_manager=auth_manager)

    def login_page(self, error: str = "", next_url: str = "/") -> bytes:
        return render_login_page(error, next_url, inject_csrf=_inject_admin_csrf)

    def render_user_management(self, notices: Optional[list[str]] = None) -> bytes:
        return render_admin_user_management(self.auth_manager, page_renderer=html_page, notices=notices)

    def user_create_form(self, _args: argparse.Namespace, fields: Optional[dict] = None, error: str = "") -> str:
        return user_create_form(fields, error)

    def user_json_form(self, key: str, rec: dict, error: str = "") -> str:
        return user_json_form(key, rec, error)

class AdminHandler(
    AuthSystemControllerMixin, MegaNetControllerMixin, TelebradescoControllerMixin,
    DashboardControllerMixin, SettingsControllerMixin, AdminBaseHandler,
):
    """Composed administrative HTTP handler."""

    app_context: "AdminApplicationContext"

    def _get_favicon_ico(self, parsed, current_user=None) -> None:
        candidates = [
            pathlib.Path(__file__).resolve().parent.parent / "favicon.ico",
            pathlib.Path.cwd() / "favicon.ico",
        ]
        for favicon_path in candidates:
            try:
                if favicon_path.exists() and favicon_path.is_file():
                    return self._send(200, favicon_path.read_bytes(), "image/x-icon")
            except OSError as exc:
                logging.warning("FAVICON_READ_FAILED path=%s error=%s", favicon_path, exc)
        return self._send(404, b"", "image/x-icon")

    def _get_logo_side_brand_png(self, parsed, current_user=None) -> None:
        candidates = [
            pathlib.Path(__file__).resolve().parent.parent / "assets" / "logo_side_brand.png",
            pathlib.Path(__file__).resolve().parent.parent / "logo_side_brand_transparent_480.png",
            pathlib.Path.cwd() / "logo_side_brand_transparent_480.png",
            pathlib.Path(__file__).resolve().parent.parent / "logo_side_brand.png",
            pathlib.Path.cwd() / "logo_side_brand.png",
        ]
        for logo_path in candidates:
            try:
                if logo_path.exists() and logo_path.is_file():
                    return self._send(200, logo_path.read_bytes(), "image/png")
            except OSError as exc:
                logging.warning("SIDE_BRAND_LOGO_READ_FAILED path=%s error=%s", logo_path, exc)
        return self._send(200, _SIDE_BRAND_LOGO_PNG, "image/png")

    def _get_login(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        next_url = str(query.get("next", ["/"])[0] or "/")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        current_user = self._current_admin_user()
        if current_user:
            _set_admin_web_current_user(current_user)
            return self._redirect(next_url)
        return self._send(200, self.app_context.login_page(next_url=next_url))

    def _post_logout(self, parsed, form, current_user=None) -> None:
        token = _get_cookie_value(self.headers.get("Cookie", ""), ADMIN_WEB_SESSION_COOKIE)
        if token:
            self.app_context.auth_manager.destroy_session(token)
        return self._redirect_with_cookie("/login", f"{ADMIN_WEB_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def _post_login(self, parsed, form, current_user=None) -> None:
        username = str(form.get("username", [""])[0]).strip()
        password = str(form.get("password", [""])[0])
        next_url = str(form.get("next", ["/"])[0] or "/")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        try:
            with self.app_context.auth_manager.locked_users() as data:
                rec = data.get("users", {}).get(username) if username else None
                if not isinstance(rec, dict) or not bool(rec.get("active", True)) or not _admin_password_verify(password, rec.get("password_hash")):
                    logging.warning("ADMIN_WEB_LOGIN_FAILED username=%s client=%s", username, self.client_address)
                    return self._send(401, self.app_context.login_page("Usuário ou senha inválidos.", next_url=next_url))
                token = self.app_context.auth_manager.create_session(username)
                rec["last_login_at"] = utc_now_iso()
                rec["last_login_peer"] = self.address_string()
                rec["updated_at"] = rec.get("updated_at") or utc_now_iso()
                try:
                    self.app_context.auth_manager.write_users(data)
                except (OSError, TypeError, ValueError) as exc:
                    self.app_context.auth_manager.destroy_session(token)
                    logging.error("ADMIN_WEB_LOGIN_PERSIST_FAILED username=%s error=%s", username, exc)
                    return self._send(
                        500,
                        self.app_context.login_page(
                            "Não foi possível salvar os dados do login administrativo.",
                            next_url=next_url,
                        ),
                    )
        except (OSError, ValueError) as exc:
            logging.error("ADMIN_WEB_LOGIN_REGISTRY_UNAVAILABLE username=%s error=%s", username, exc)
            return self._send(
                503,
                self.app_context.login_page(
                    "Cadastro de usuários administrativos indisponível.",
                    next_url=next_url,
                ),
            )
        cookie = f"{ADMIN_WEB_SESSION_COOKIE}={urllib.parse.quote(token)}; Path=/; HttpOnly; SameSite=Lax"
        logging.info("ADMIN_WEB_LOGIN_OK username=%s role=%s client=%s", username, _normalize_admin_role(rec.get("role")), self.client_address)
        return self._redirect_with_cookie(next_url, cookie)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        public_handler_name = ADMIN_GET_PUBLIC_ROUTES.get(path)
        if public_handler_name:
            return getattr(self, public_handler_name)(parsed, None)

        current_user = self._current_admin_user()
        _set_admin_web_current_user(current_user)
        if not current_user:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            return self._redirect("/login?next=" + urllib.parse.quote(target, safe="/?:=&"))

        handler_name = ADMIN_GET_PRIVATE_ROUTES.get(path)
        if handler_name:
            return getattr(self, handler_name)(parsed, current_user)
        if path == "/meganet":
            return self._get_dashboard(parsed, current_user, meganet_only=True)
        return self._get_dashboard(parsed, current_user, meganet_only=False)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            form = self._form()
        except (OSError, ValueError) as exc:
            logging.warning("ADMIN_BAD_POST path=%s client=%s error=%s", parsed.path, self.client_address, exc)
            return self._send(400, html_page("Requisição inválida", "<p class='warn'>Não foi possível interpretar os dados enviados.</p>"))

        supplied_csrf = str(form.get("_csrf", [""])[0])
        if not secrets.compare_digest(supplied_csrf, _ADMIN_CSRF_TOKEN):
            logging.warning("ADMIN_CSRF_REJECT path=%s client=%s", parsed.path, self.client_address)
            return self._send(403, html_page("Requisição recusada", "<p class='warn'>Token de segurança ausente ou inválido. Recarregue a página e tente novamente.</p>"))

        if parsed.path == "/logout":
            return self._post_logout(parsed, form, None)

        if parsed.path == "/login":
            return self._post_login(parsed, form, None)

        current_user = self._current_admin_user()
        _set_admin_web_current_user(current_user)
        if not current_user:
            return self._redirect("/login")
        if not self._is_admin_user(current_user):
            return self._send(403, html_page("Somente leitura", "<p class='warn'>Seu perfil permite apenas visualizar informações. Alterações exigem perfil Administrador.</p>"))

        handler_name = ADMIN_POST_PRIVATE_ROUTES.get(parsed.path)
        if handler_name:
            return getattr(self, handler_name)(parsed, form, current_user)
        return self._send(404, html_page("404", "<p>Rota não encontrada.</p>"))


def start_admin_server(args: argparse.Namespace) -> Optional[ThreadingHTTPServer]:
    """Start the administrative HTTP server for *args*."""
    if not getattr(args, "admin_web", True):
        return None

    try:
        app_context = AdminApplicationContext.create(args)
    except (OSError, ValueError) as exc:
        logging.error("ADMIN_WEB_CONTEXT_INIT_FAILED error=%s", exc)
        return None

    class BoundAdminHandler(AdminHandler):
        pass

    BoundAdminHandler.app_context = app_context
    BoundAdminHandler.auth_manager = app_context.auth_manager

    missing_routes = validate_route_table(BoundAdminHandler)
    if missing_routes:
        logging.error("ADMIN_ROUTE_TABLE_INVALID missing=%s", ",".join(missing_routes))
        return None

    try:
        admin_bind_host = _normalize_bind_host(getattr(args, "admin_host", "127.0.0.1"))
        args.admin_host = admin_bind_host
        httpd = ThreadingHTTPServer((admin_bind_host, int(args.admin_port)), BoundAdminHandler)
    except (OSError, TypeError, ValueError) as exc:
        logging.error(
            "ADMIN_WEB_START_FAILED host=%s port=%s error=%s",
            getattr(args, "admin_host", ""), getattr(args, "admin_port", ""), exc,
        )
        return None

    thread = threading.Thread(target=httpd.serve_forever, name="MegaNetAdminWeb", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        logging.error(
            "ADMIN_WEB_THREAD_START_FAILED host=%s port=%s error=%s",
            args.admin_host, args.admin_port, exc,
        )
        httpd.server_close()
        return None
    logging.info("MegaNet admin web listening on http://%s:%s", args.admin_host, args.admin_port)
    return httpd
