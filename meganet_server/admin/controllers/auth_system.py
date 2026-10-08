#!/usr/bin/env python3
"""Authentication-adjacent and system administrative controllers."""

from __future__ import annotations

import html
import json
import urllib.parse

from ..auth import (
    normalize_role as _normalize_admin_role, password_hash as _admin_password_hash,
)
from ..page import html_page
from ..statistics import render_system_access_history, render_system_statistics
from ...common import utc_now_iso
from ...config import current_config_from_args
from ...meganet.accounts import normalize_user_key
from ...meganet.content_store import current_content_from_args, reload_active_meganet_content
from ...runtime.statistics import _runtime_access_date_range, _runtime_query_statistics


class AuthSystemControllerMixin:
    """Route controller mixin; requires :class:`AdminBaseHandler` methods."""

    def _get_system_users(self, parsed, current_user=None) -> None:
        if not self._is_admin_user(current_user):
            return self._send(403, html_page("Acesso negado", "<p class='warn'>Apenas administradores podem acessar a gerência do sistema.</p>"))
        return self._send(200, self.app_context.render_user_management())

    def _get_system_stats(self, parsed, current_user=None) -> None:
        return self._send(200, render_system_statistics(
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
            query_statistics=_runtime_query_statistics,
            page_renderer=html_page,
        ))

    def _get_system_stats_history(self, parsed, current_user=None) -> None:
        return self._send(200, render_system_access_history(
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
            query_statistics=_runtime_query_statistics,
            access_date_range=_runtime_access_date_range,
            page_renderer=html_page,
        ))

    def _get_config_json(self, parsed, current_user=None) -> None:
        payload = json.dumps(current_config_from_args(self.app_context.args), ensure_ascii=False, indent=2).encode("utf-8")
        return self._send(200, payload, "application/json; charset=utf-8")

    def _get_content_json(self, parsed, current_user=None) -> None:
        reload_active_meganet_content(self.app_context.args, create_if_missing=True)
        payload = json.dumps(current_content_from_args(self.app_context.args), ensure_ascii=False, indent=2).encode("utf-8")
        return self._send(200, payload, "application/json; charset=utf-8")

    def _post_system_users_create(self, parsed, form, current_user=None) -> None:
        with self.app_context.auth_manager.locked_users() as data:
            users = data.setdefault("users", {})
            username = normalize_user_key(form.get("username", [""])[0])
            password = str(form.get("password", [""])[0])
            role = _normalize_admin_role(form.get("role", ["user"])[0])
            if not username or not password:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>Usuário e senha são obrigatórios.</p>"]))
            if username in users:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>Usuário já existe.</p>"]))
            now = utc_now_iso()
            users[username] = {"username": username, "role": role, "active": True, "created_at": now, "updated_at": now, "password_hash": _admin_password_hash(password)}
            self.app_context.auth_manager.write_users(data)
            return self._send(200, self.app_context.render_user_management([f"<p class='ok'>Usuário {html.escape(username)} criado.</p>"]))

    def _post_system_users_bulk(self, parsed, form, current_user=None) -> None:
        with self.app_context.auth_manager.locked_users() as data:
            users = data.setdefault("users", {})
            selected = [str(x).strip() for x in form.get("selected", []) if str(x).strip()]
            action = str(form.get("action", ["save"])[0] or "save").strip().lower()
            if not selected:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>Selecione ao menos um usuário.</p>"]))
            missing = [u for u in selected if u not in users]
            if missing:
                return self._send(404, self.app_context.render_user_management(["<p class='warn'>Um ou mais usuários selecionados não foram encontrados.</p>"]))
            current_username = str((current_user or {}).get("username") or "")
            if action == "delete":
                if current_username in selected:
                    return self._send(400, self.app_context.render_user_management(["<p class='warn'>Não é possível excluir o usuário atualmente logado.</p>"]))
                remaining_admins = [u for u, rec in users.items() if u not in selected and isinstance(rec, dict) and bool(rec.get("active", True)) and _normalize_admin_role(rec.get("role")) == "admin"]
                deleting_admins = [u for u in selected if isinstance(users.get(u), dict) and _normalize_admin_role(users[u].get("role")) == "admin"]
                if deleting_admins and not remaining_admins:
                    return self._send(400, self.app_context.render_user_management(["<p class='warn'>Não é possível excluir o último administrador ativo.</p>"]))
                for username in selected:
                    users.pop(username, None)
                self.app_context.auth_manager.write_users(data)
                return self._send(200, self.app_context.render_user_management([f"<p class='ok'>{len(selected)} usuário(s) excluído(s).</p>"]))
            if action != "save":
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>Ação inválida.</p>"]))
            updates = 0
            for username in selected:
                rec = users.get(username)
                if not isinstance(rec, dict):
                    continue
                role = _normalize_admin_role(form.get(f"role__{username}", [rec.get("role", "user")])[0])
                active = str(form.get(f"active__{username}", ["true" if bool(rec.get("active", True)) else "false"])[0]).lower() == "true"
                rec["role"] = role
                rec["active"] = active
                password = str(form.get(f"password__{username}", [""])[0])
                if password:
                    rec["password_hash"] = _admin_password_hash(password)
                rec["updated_at"] = utc_now_iso()
                users[username] = rec
                updates += 1
            active_admins = [u for u, rec in users.items() if isinstance(rec, dict) and bool(rec.get("active", True)) and _normalize_admin_role(rec.get("role")) == "admin"]
            if not active_admins:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>É necessário manter ao menos um administrador ativo.</p>"]))
            self.app_context.auth_manager.write_users(data)
            return self._send(200, self.app_context.render_user_management([f"<p class='ok'>{updates} usuário(s) atualizado(s).</p>"]))

    def _post_system_users_update(self, parsed, form, current_user=None) -> None:
        with self.app_context.auth_manager.locked_users() as data:
            users = data.setdefault("users", {})
            username = str(form.get("username", [""])[0]).strip()
            rec = users.get(username)
            if not isinstance(rec, dict):
                return self._send(404, self.app_context.render_user_management(["<p class='warn'>Usuário não encontrado.</p>"]))
            role = _normalize_admin_role(form.get("role", [rec.get("role", "user")])[0])
            active = str(form.get("active", ["true"])[0]).lower() == "true"
            candidate = dict(rec)
            candidate["role"] = role
            candidate["active"] = active
            password = str(form.get("password", [""])[0])
            if password:
                candidate["password_hash"] = _admin_password_hash(password)
            candidate["updated_at"] = utc_now_iso()
            active_admins = [
                user_key for user_key, user_rec in users.items()
                if isinstance(user_rec, dict)
                and bool((candidate if user_key == username else user_rec).get("active", True))
                and _normalize_admin_role((candidate if user_key == username else user_rec).get("role")) == "admin"
            ]
            if not active_admins:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>É necessário manter ao menos um administrador ativo.</p>"]))
            users[username] = candidate
            self.app_context.auth_manager.write_users(data)
            return self._send(200, self.app_context.render_user_management([f"<p class='ok'>Usuário {html.escape(username)} atualizado.</p>"]))

    def _post_system_users_delete(self, parsed, form, current_user=None) -> None:
        with self.app_context.auth_manager.locked_users() as data:
            users = data.setdefault("users", {})
            username = str(form.get("username", [""])[0]).strip()
            if username not in users:
                return self._send(404, self.app_context.render_user_management(["<p class='warn'>Usuário não encontrado.</p>"]))
            remaining_admins = [u for u, rec in users.items() if u != username and isinstance(rec, dict) and bool(rec.get("active", True)) and _normalize_admin_role(rec.get("role")) == "admin"]
            if _normalize_admin_role(users[username].get("role")) == "admin" and not remaining_admins:
                return self._send(400, self.app_context.render_user_management(["<p class='warn'>Não é possível excluir o último administrador ativo.</p>"]))
            users.pop(username, None)
            self.app_context.auth_manager.write_users(data)
            return self._send(200, self.app_context.render_user_management([f"<p class='ok'>Usuário {html.escape(username)} excluído.</p>"]))
