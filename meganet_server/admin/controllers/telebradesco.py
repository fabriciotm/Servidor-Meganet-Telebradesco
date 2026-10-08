#!/usr/bin/env python3
"""Telebradesco administrative controllers."""

from __future__ import annotations

import copy
import html
import json
import pathlib
import urllib.parse

from ..page import html_page
from ...common import utc_now_iso
from ..telebradesco import (
    render_telebradesco_accounts_index, render_telebradesco_editor,
    telebradesco_content_from_form,
)
from ...telebradesco.accounts import (
    DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE, DEFAULT_TELEBRADESCO_ACCOUNTS_FILE,
    TelebradescoAccountsError, read_telebradesco_accounts, telebradesco_account_from_form,
    telebradesco_account_key, update_telebradesco_accounts,
)
from ...telebradesco.content import (
    blank_telebradesco_content, compile_telebradesco_content, normalize_telebradesco_content,
    validate_telebradesco_content,
)

class TelebradescoControllerMixin:
    """Route controller mixin; requires :class:`AdminBaseHandler` methods."""

    def _get_telebradesco(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        try:
            store = read_telebradesco_accounts(accounts_path, accounts_content_file=tele_path, create_if_missing=True)
        except (TelebradescoAccountsError, OSError, TypeError, ValueError) as exc:
            backup_line = (
                "<p>Uma cópia de segurança foi criada para preservar o conteúdo anterior.</p>"
                if getattr(exc, "backup_path", None) else ""
            )
            body = (
                "<div class='card'><h3>Banco de contas indisponível</h3>"
                "<p class='warn'>O banco de contas Telebradesco está inválido ou indisponível.</p>"
                "<p>O conteúdo original foi preservado. Enquanto ele não for corrigido, "
                "o Telebradesco responderá com falha de comunicação.</p>"
                f"{backup_line}</div>"
            )
            return self._send(503, html_page("Contas Telebradesco", body))

        accounts = store.get("accounts", {}) if isinstance(store.get("accounts"), dict) else {}
        is_new = bool(query.get("new"))
        requested_key = str(query.get("account", [""])[0]).strip()
        notices: list[str] = []
        if query.get("saved"):
            notices.append("<p class='ok'><b>Identificação e conteúdo salvos em arquivos separados.</b> Serão usados na próxima conexão.</p>")
        if query.get("deleted"):
            notices.append("<p class='ok'><b>Conta excluída.</b></p>")
        if query.get("blocked"):
            notices.append("<p class='ok'><b>Conta bloqueada.</b></p>")
        if query.get("unblocked"):
            notices.append("<p class='ok'><b>Conta desbloqueada.</b></p>")
        if query.get("cleared"):
            notices.append("<p class='ok'><b>Dados da conta limpos.</b></p>")
        if query.get("json_saved"):
            notices.append("<p class='ok'><b>Conteúdo salvo para a conta.</b></p>")
        if query.get("error"):
            notices.append(f"<p class='warn'><b>Erro:</b> {html.escape(query.get('error',[''])[0])}</p>")

        if not is_new and not requested_key:
            body = render_telebradesco_accounts_index(
                accounts,
                accounts_path=accounts_path,
                accounts_content_path=tele_path,
                active_service=getattr(self.app_context.args, "active_service", "meganet"),
                notices=notices,
            )
            return self._send(200, html_page("Contas Telebradesco", body))

        if is_new:
            account_key = ""
            account = {
                "nome": "",
                "agencia": "",
                "conta": "",
                "digito": "",
                "senha": "",
                "bloqueada": False,
                "content": blank_telebradesco_content(),
            }
            title = "Nova conta Telebradesco"
        else:
            if requested_key not in accounts:
                body = render_telebradesco_accounts_index(
                    accounts,
                    accounts_path=accounts_path,
                    accounts_content_path=tele_path,
                    active_service=getattr(self.app_context.args, "active_service", "meganet"),
                    notices=["<p class='warn'><b>Conta não encontrada.</b> Selecione uma conta da lista.</p>"],
                )
                return self._send(404, html_page("Conta não encontrada", body))
            account_key = requested_key
            account = accounts[account_key]
            title = "Editar conta Telebradesco"

        tele_content = blank_telebradesco_content() if is_new else normalize_telebradesco_content(account.get("content"))
        compiled = compile_telebradesco_content(tele_content)
        body = render_telebradesco_editor(
            tele_content,
            compiled,
            tele_path=tele_path,
            accounts_path=accounts_path,
            active_service=getattr(self.app_context.args, "active_service", "meganet"),
            notices=notices,
            account_key=account_key,
            account=account,
            is_new=is_new,
        )
        return self._send(200, html_page(title, body))

    def _post_telebradesco_save(self, parsed, form, current_user=None) -> None:
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        original_key = str(form.get("original_account_key", [""])[0]).strip()
        try:
            content = telebradesco_content_from_form(form)
            account = telebradesco_account_from_form(form, content)
            new_key = telebradesco_account_key(account["agencia"], account["conta"], account["digito"])

            def _save_account(store: dict) -> str:
                accounts = store.setdefault("accounts", {})
                if new_key != original_key and new_key in accounts:
                    raise ValueError(f"A conta {new_key} já está cadastrada.")
                previous_account = accounts.get(original_key or new_key) if isinstance(accounts, dict) else None
                if original_key and original_key != new_key:
                    if original_key not in accounts:
                        raise ValueError("A conta original não foi encontrada; recarregue a página.")
                    previous_account = accounts.pop(original_key)
                if isinstance(previous_account, dict):
                    for stat_key in ("total_accesses", "last_access_at", "last_peer", "access_history"):
                        if stat_key in previous_account:
                            account[stat_key] = copy.deepcopy(previous_account.get(stat_key))
                accounts[new_key] = account
                return new_key

            update_telebradesco_accounts(
                accounts_path,
                _save_account,
                accounts_content_file=tele_path,
            )
        except Exception as exc:
            destination = "/telebradesco?error=" + urllib.parse.quote(str(exc))
            if original_key:
                destination += "&account=" + urllib.parse.quote(original_key)
            else:
                destination += "&new=1"
            return self._redirect(destination)
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        if runtime and str(getattr(self.app_context.args, "active_service", "meganet")) == "telebradesco":
            runtime.request_close_all("conta Telebradesco atualizada")
        return self._redirect("/telebradesco?saved=1&account=" + urllib.parse.quote(new_key))

    def _post_telebradesco_block(self, parsed, form, current_user=None) -> None:
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        account_key = str(form.get("account_key", [""])[0]).strip()
        blocked_value = str(form.get("blocked", ["true"])[0]).strip().lower() in {"1", "true", "yes", "on", "sim"}
        try:
            def _block_account(store: dict) -> str:
                accounts = store.setdefault("accounts", {})
                if account_key not in accounts:
                    raise ValueError("Conta não encontrada.")
                if not isinstance(accounts[account_key], dict):
                    raise ValueError("Conta inválida.")
                accounts[account_key]["bloqueada"] = bool(blocked_value)
                accounts[account_key]["updated_at"] = utc_now_iso()
                return account_key

            update_telebradesco_accounts(
                accounts_path,
                _block_account,
                accounts_content_file=tele_path,
            )
        except Exception as exc:
            return self._redirect("/telebradesco?error=" + urllib.parse.quote(str(exc)))
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        if runtime and str(getattr(self.app_context.args, "active_service", "meganet")) == "telebradesco":
            runtime.request_close_all("bloqueio Telebradesco alterado")
        notice_key = "blocked" if blocked_value else "unblocked"
        return self._redirect("/telebradesco?" + notice_key + "=1")

    def _post_telebradesco_clear_data(self, parsed, form, current_user=None) -> None:
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        account_key = str(form.get("account_key", [""])[0]).strip()
        try:
            def _clear_account_data(store: dict) -> str:
                accounts = store.setdefault("accounts", {})
                if account_key not in accounts:
                    raise ValueError("Conta não encontrada.")
                if not isinstance(accounts[account_key], dict):
                    raise ValueError("Conta inválida.")
                accounts[account_key]["content"] = blank_telebradesco_content()
                accounts[account_key]["updated_at"] = utc_now_iso()
                return account_key

            update_telebradesco_accounts(
                accounts_path,
                _clear_account_data,
                accounts_content_file=tele_path,
            )
        except Exception as exc:
            destination = "/telebradesco?error=" + urllib.parse.quote(str(exc))
            if account_key:
                destination += "&account=" + urllib.parse.quote(account_key)
            return self._redirect(destination)
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        if runtime and str(getattr(self.app_context.args, "active_service", "meganet")) == "telebradesco":
            runtime.request_close_all("dados da conta Telebradesco limpos")
        return self._redirect("/telebradesco?cleared=1&account=" + urllib.parse.quote(account_key))

    def _post_telebradesco_delete(self, parsed, form, current_user=None) -> None:
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        account_key = str(form.get("account_key", [""])[0]).strip()
        try:
            def _delete_account(store: dict) -> str:
                accounts = store.setdefault("accounts", {})
                if account_key not in accounts:
                    raise ValueError("Conta não encontrada.")
                if len(accounts) <= 1:
                    raise ValueError("O banco deve manter pelo menos uma conta.")
                accounts.pop(account_key)
                return account_key

            update_telebradesco_accounts(
                accounts_path,
                _delete_account,
                accounts_content_file=tele_path,
            )
        except Exception as exc:
            return self._redirect("/telebradesco?error=" + urllib.parse.quote(str(exc)))
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        if runtime and str(getattr(self.app_context.args, "active_service", "meganet")) == "telebradesco":
            runtime.request_close_all("conta Telebradesco excluída")
        return self._redirect("/telebradesco?deleted=1")

    def _post_telebradesco_save_json(self, parsed, form, current_user=None) -> None:
        tele_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        accounts_path = pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        account_key = str(form.get("account_key", [""])[0]).strip()
        raw = form.get("json_body", [""])[0]
        try:
            value = json.loads(raw)
            normalized = validate_telebradesco_content(value)
            compile_telebradesco_content(normalized)
            def _save_account_json(store: dict) -> str:
                accounts = store.setdefault("accounts", {})
                if account_key not in accounts:
                    raise ValueError("Conta não encontrada para salvar o JSON.")
                accounts[account_key]["content"] = normalized
                return account_key

            update_telebradesco_accounts(
                accounts_path,
                _save_account_json,
                accounts_content_file=tele_path,
            )
        except Exception as exc:
            return self._redirect("/telebradesco?error=" + urllib.parse.quote(str(exc)) + "&account=" + urllib.parse.quote(account_key))
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        if runtime and str(getattr(self.app_context.args, "active_service", "meganet")) == "telebradesco":
            runtime.request_close_all("conteúdo Telebradesco atualizado")
        return self._redirect("/telebradesco?json_saved=1&account=" + urllib.parse.quote(account_key))

