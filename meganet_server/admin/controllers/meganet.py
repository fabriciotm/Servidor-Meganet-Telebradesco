#!/usr/bin/env python3
"""MegaNet user, access-code and mailbox administrative controllers."""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import pathlib
import urllib.parse

from ..meganet import (
    _cadastro_transaction, _find_duplicate_field, _format_user_edit_form, _mailbox_rows, _read_cadastro_db,
    _read_cadastro_users, _write_cadastro_db,
)
from ..page import html_page
from ...common import utc_now_iso
from ...meganet.accounts import (
    FIELD_NAMES, generate_random_access_code, meganet_email_for_name,
    normalize_email_address, normalize_user_key, validate_access_code,
)
from ...meganet.content import MAX_MESSAGE_LINE_LEN
from ...meganet.mail import MailStore

class MegaNetControllerMixin:
    """Route controller mixin; requires :class:`AdminBaseHandler` methods."""

    def _get_mailboxes(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        store = MailStore(
            pathlib.Path(self.app_context.args.mail_store_dir),
            getattr(self.app_context.args, "mail_inbox", None),
            mail_domain=self.app_context.args.mail_domain,
            admin_email=self.app_context.args.admin_email,
        )
        rows = []
        total_messages = 0
        for email_addr, mailbox_path, count in _mailbox_rows(store):
            latest = ""
            try:
                data = json.loads(mailbox_path.read_text(encoding="utf-8"))
                messages = data.get("messages", []) if isinstance(data, dict) else []
                if isinstance(messages, list) and messages:
                    last = messages[-1] if isinstance(messages[-1], dict) else {}
                    latest = str(last.get("subject") or last.get("id") or "")
            except (OSError, UnicodeError, json.JSONDecodeError):
                latest = ""
            total_messages += int(count or 0)
            quoted = urllib.parse.quote(str(email_addr))
            rows.append(
                "<tr>"
                f"<td><a href='/mailbox?email={quoted}'>{html.escape(str(email_addr))}</a></td>"
                f"<td>{int(count or 0)}</td>"
                f"<td>{html.escape(latest or '-')}</td>"
                "<td>"
                f"<form method='post' action='/mailbox/clear' onsubmit='return confirm(\"Limpar esta caixa postal?\");'><input type='hidden' name='email' value='{html.escape(str(email_addr), quote=True)}'><button class='secondary' type='submit'>Limpar</button></form>"
                "</td>"
                "</tr>"
            )
        notice = "<p class='ok'><b>Mensagem enviada.</b></p>" if query.get("sent") else ""
        listing = (
            "<table><thead><tr><th>Caixa postal</th><th>Mensagens</th><th>Último assunto</th><th>Ação</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            if rows else "<div class='empty-state'>Nenhuma caixa postal encontrada.</div>"
        )
        body = f"""
        {f"<div class='card'>{notice}</div>" if notice else ""}
        <div class='grid-compact'>
          <div class='metric'><strong>{len(rows)}</strong><span>caixas postais</span></div>
          <div class='metric'><strong>{total_messages}</strong><span>mensagens armazenadas</span></div>
          <div class='metric'><strong>{html.escape(str(self.app_context.args.admin_email))}</strong><span>remetente padrão</span></div>
        </div>
        <div class='card'>
          <div class='section-header'><div><h2>Correio MegaNet</h2><p class='muted'>Caixas postais dos usuários e mensagens pendentes para entrega.</p></div></div>
          {listing}
        </div>
        <div class='card'>
          <h3>Enviar mensagem</h3>
          <form method='post' action='/mail/send'>
            <div class='grid'>
              <label>Para<input name='to' placeholder='usuario@{html.escape(self.app_context.args.mail_domain)}'></label>
              <label>Assunto<input name='subject' value='MEGA NET' maxlength='40'></label>
            </div>
            <label>Mensagem<textarea name='body' data-maxline='{MAX_MESSAGE_LINE_LEN}' placeholder='Digite a mensagem'></textarea></label>
            <p class='muted'>Use linhas curtas. Use no máximo 35 caracteres por linha; 36 caracteres provocam uma linha em branco na ROM.</p>
            <button class='okbtn' type='submit'>Enviar mensagem</button>
          </form>
        </div>"""
        return self._send(200, html_page("Correio MegaNet", body))

    def _get_mailbox(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        email_addr = normalize_email_address(query.get("email", [""])[0], self.app_context.args.mail_domain)
        if not email_addr:
            return self._redirect("/mailboxes")
        store = MailStore(
            pathlib.Path(self.app_context.args.mail_store_dir),
            getattr(self.app_context.args, "mail_inbox", None),
            mail_domain=self.app_context.args.mail_domain,
            admin_email=self.app_context.args.admin_email,
        )
        messages = store.load_mailbox_messages(email_addr)
        rows = []
        quoted_email = urllib.parse.quote(email_addr)
        for msg_index, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            body_lines = msg.get("body", [])
            if isinstance(body_lines, str):
                body_lines = body_lines.splitlines()
            preview = " / ".join(str(x) for x in (body_lines if isinstance(body_lines, list) else [])[:3])
            open_url = f"/mailbox/message?email={quoted_email}&index={msg_index}"
            rows.append(
                "<tr>"
                f"<td>{msg_index + 1}</td>"
                f"<td>{html.escape(str(msg.get('timestamp') or ''))}</td>"
                f"<td>{html.escape(str(msg.get('from') or ''))}</td>"
                f"<td>{html.escape(str(msg.get('subject') or ''))}</td>"
                f"<td>{html.escape(preview)}</td>"
                f"<td><a class='button secondary' href='{open_url}'>Abrir</a></td>"
                "</tr>"
            )
        listing = (
            "<table><thead><tr><th>#</th><th>Data</th><th>De</th><th>Assunto</th><th>Prévia</th><th>Ação</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            if rows else "<div class='empty-state'>Esta caixa postal não possui mensagens pendentes.</div>"
        )
        body = f"""
        <div class='breadcrumb'><a href='/mailboxes'>Correio</a><span>›</span><b>{html.escape(email_addr)}</b></div>
        <div class='card'>
          <div class='section-header'><div><h2>{html.escape(email_addr)}</h2><p class='muted'>{len(rows)} mensagem(ns) pendente(s).</p></div><a class='button secondary' href='/mailboxes'>Voltar</a></div>
          {listing}
        </div>
        <div class='card'>
          <form method='post' action='/mailbox/clear' onsubmit='return confirm("Limpar esta caixa postal?");'>
            <input type='hidden' name='email' value='{html.escape(email_addr, quote=True)}'>
            <button class='warnbtn' type='submit'>Limpar caixa postal</button>
          </form>
        </div>"""
        return self._send(200, html_page("Caixa postal MegaNet", body))

    def _get_mailbox_message(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        email_addr = normalize_email_address(query.get("email", [""])[0], self.app_context.args.mail_domain)
        if not email_addr:
            return self._redirect("/mailboxes")
        try:
            msg_index = int(query.get("index", ["0"])[0])
        except (TypeError, ValueError):
            msg_index = -1
        store = MailStore(
            pathlib.Path(self.app_context.args.mail_store_dir),
            getattr(self.app_context.args, "mail_inbox", None),
            mail_domain=self.app_context.args.mail_domain,
            admin_email=self.app_context.args.admin_email,
        )
        messages = store.load_mailbox_messages(email_addr)
        if msg_index < 0 or msg_index >= len(messages) or not isinstance(messages[msg_index], dict):
            return self._send(404, html_page("Mensagem não encontrada", f"<p class='warn'>Mensagem não encontrada em {html.escape(email_addr)}.</p><p><a href='/mailbox?email={urllib.parse.quote(email_addr)}'>Voltar</a></p>"))
        msg = messages[msg_index]
        body_value = msg.get("body", [])
        if isinstance(body_value, str):
            body_text = body_value
        elif isinstance(body_value, list):
            body_text = "\n".join(str(x) for x in body_value)
        else:
            body_text = str(body_value or "")
        meta_rows = "".join([
            f"<tr><th>Data</th><td>{html.escape(str(msg.get('timestamp') or ''))}</td></tr>",
            f"<tr><th>De</th><td>{html.escape(str(msg.get('from') or ''))}</td></tr>",
            f"<tr><th>Para</th><td>{html.escape(str(msg.get('to') or email_addr))}</td></tr>",
            f"<tr><th>Assunto</th><td>{html.escape(str(msg.get('subject') or ''))}</td></tr>",
            f"<tr><th>Tipo</th><td>{html.escape(str(msg.get('type') or ''))}</td></tr>",
            f"<tr><th>Status</th><td>{html.escape(str(msg.get('delivery_status') or ''))}</td></tr>",
        ])
        body = f"""
        <div class='breadcrumb'><a href='/mailboxes'>Correio</a><span>›</span><a href='/mailbox?email={urllib.parse.quote(email_addr)}'>{html.escape(email_addr)}</a><span>›</span><b>Mensagem {msg_index + 1}</b></div>
        <div class='card'>
          <div class='section-header'><div><h2>{html.escape(str(msg.get('subject') or 'Mensagem'))}</h2><p class='muted'>Mensagem completa da caixa postal.</p></div><a class='button secondary' href='/mailbox?email={urllib.parse.quote(email_addr)}'>Voltar</a></div>
          <table><tbody>{meta_rows}</tbody></table>
        </div>
        <div class='card'>
          <h3>Conteúdo</h3>
          <div class='mail-preview'>{html.escape(body_text)}</div>
        </div>"""
        return self._send(200, html_page("Mensagem MegaNet", body))

    def _get_users(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        users_data = _read_cadastro_users(db_path)
        rows = []
        total_sessions = 0
        for key, rec in sorted(users_data.items()):
            fields = rec.get("fields", {}) if isinstance(rec, dict) else {}
            nome = str(fields.get("nome") or "")
            nome_meganet = str(fields.get("nome_meganet") or key)
            email_addr = str(fields.get("email_meganet") or meganet_email_for_name(nome_meganet, self.app_context.args.mail_domain))
            codigo = str(fields.get("codigo_acesso") or "")
            sessions = int(rec.get("total_sessions") or 0) if isinstance(rec, dict) else 0
            total_sessions += sessions
            quoted = urllib.parse.quote(str(key))
            rows.append(
                "<tr>"
                f"<td><a href='/user?key={quoted}'>{html.escape(nome_meganet)}</a></td>"
                f"<td>{html.escape(nome or '-')}</td>"
                f"<td>{html.escape(email_addr)}</td>"
                f"<td>{html.escape(codigo or '-')}</td>"
                f"<td>{sessions}</td>"
                f"<td><a class='button secondary' href='/user?key={quoted}'>Editar</a> <a class='button secondary' href='/user?key={quoted}&json=1'>JSON</a></td>"
                "</tr>"
            )
        listing = (
            "<table><thead><tr><th>Usuário</th><th>Nome</th><th>E-mail MegaNet</th><th>Código de acesso</th><th>Acessos</th><th>Ação</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            if rows else "<div class='empty-state'>Nenhum usuário cadastrado.</div>"
        )
        notice = ""
        if query.get("created"):
            notice = "<div class='card'><p class='ok'><b>Usuário criado.</b></p></div>"
        body = f"""
        {notice}
        <div class='grid-compact'>
          <div class='metric'><strong>{len(rows)}</strong><span>usuários cadastrados</span></div>
          <div class='metric'><strong>{total_sessions}</strong><span>acessos registrados</span></div>
          <div class='metric'><strong>{html.escape(str(self.app_context.args.mail_domain))}</strong><span>domínio MegaNet</span></div>
        </div>
        <div class='card'>
          <div class='section-header'><div><h2>Usuários MegaNet</h2><p class='muted'>Cadastro usado para login, correio e histórico de acessos.</p></div><a class='button okbtn' href='/user?new=1'>Criar usuário</a></div>
          {listing}
        </div>"""
        return self._send(200, html_page("Usuários MegaNet", body))

    def _get_user(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("new"):
            return self._send(200, html_page("Criar usuário MegaNet", self.app_context.user_create_form(self.app_context.args)))
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        users_data = _read_cadastro_users(db_path)
        key = str(query.get("key", [""])[0]).strip()
        if not key or key not in users_data:
            return self._send(404, html_page("Usuário não encontrado", "<p class='warn'>Usuário não encontrado.</p><p><a href='/users'>Voltar aos usuários</a></p>"))
        if query.get("json"):
            return self._send(200, html_page("Editar JSON do usuário", self.app_context.user_json_form(key, users_data[key])))
        return self._send(200, html_page("Editar usuário MegaNet", _format_user_edit_form(key, users_data[key], self.app_context.args)))

    def _get_access_code(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        qty_raw = str(query.get("quantidade", ["10"])[0] or "10")
        try:
            qty = max(1, min(100, int(qty_raw)))
        except (TypeError, ValueError):
            qty = 10
        generated = []
        if query.get("gerar") or not query:
            generated = [generate_random_access_code() for _ in range(qty)]
        validate_raw = str(query.get("codigo", [""])[0] or "").strip()
        validation_html = ""
        if validate_raw:
            ok = validate_access_code(validate_raw)
            validation_html = f"<p class='{'ok' if ok else 'warn'}'><b>{'Código válido.' if ok else 'Código inválido.'}</b></p>"
        rows = "".join(f"<tr><td><code>{html.escape(code)}</code></td></tr>" for code in generated)
        generated_html = (
            "<table><thead><tr><th>Código gerado</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if generated else ""
        )
        body = f"""
        <div class='grid'>
          <div class='card'>
            <h3>Gerar códigos de acesso</h3>
            <form method='get' action='/access-code'>
              <label>Quantidade<input name='quantidade' type='number' min='1' max='100' value='{qty}'></label>
              <button class='okbtn' type='submit' name='gerar' value='1'>Gerar</button>
            </form>
            <p class='muted'>Os códigos gerados podem ser usados no cadastro MegaNet. Gere apenas a quantidade necessária.</p>
          </div>
          <div class='card'>
            <h3>Validar código</h3>
            <form method='get' action='/access-code'>
              <label>Código de acesso<input name='codigo' value='{html.escape(validate_raw, quote=True)}'></label>
              <button class='secondary' type='submit' name='validar' value='1'>Validar</button>
            </form>
            {validation_html}
          </div>
        </div>
        <div class='card'><h3>Códigos</h3>{generated_html if generated_html else '<p class="muted">Clique em Gerar para criar novos códigos.</p>'}</div>"""
        return self._send(200, html_page("Gerador de Código de Acesso", body))

    @_cadastro_transaction
    def _post_user_create(self, parsed, form, current_user=None) -> None:
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        data = _read_cadastro_db(db_path)
        users = data.setdefault("users", {})
        fields = {name: form.get(f"field_{name}", [""])[0].strip() for name in FIELD_NAMES}
        required = ["nome", "nome_meganet", "senha", "codigo_acesso"]
        missing = [x for x in required if not fields.get(x)]
        if missing:
            return self._send(400, html_page("Erro ao criar usuário", self.app_context.user_create_form(self.app_context.args, fields, "Campos obrigatórios ausentes: " + ", ".join(missing))))
        key = normalize_user_key(fields["nome_meganet"])
        if not key:
            return self._send(400, html_page("Erro ao criar usuário", self.app_context.user_create_form(self.app_context.args, fields, "nome_meganet inválido.")))
        if _find_duplicate_field(users, "nome_meganet", fields["nome_meganet"]):
            return self._send(400, html_page("Erro ao criar usuário", self.app_context.user_create_form(self.app_context.args, fields, "nome_meganet já cadastrado.")))
        if _find_duplicate_field(users, "codigo_acesso", fields["codigo_acesso"]) and getattr(self.app_context.args, "codigo_acesso_duplicate_action", "reject-message") != "allow":
            return self._send(400, html_page("Erro ao criar usuário", self.app_context.user_create_form(self.app_context.args, fields, "codigo_acesso já cadastrado.")))
        if not fields.get("email_meganet"):
            fields["email_meganet"] = meganet_email_for_name(fields["nome_meganet"], self.app_context.args.mail_domain)
        else:
            fields["email_meganet"] = normalize_email_address(fields["email_meganet"], self.app_context.args.mail_domain)
        if _find_duplicate_field(users, "email_meganet", fields["email_meganet"]):
            return self._send(400, html_page("Erro ao criar usuário", self.app_context.user_create_form(self.app_context.args, fields, "email_meganet já cadastrado.")))
        now = utc_now_iso()
        users[key] = {"user_key": key, "key_source": "admin-web", "created_at": now, "updated_at": now, "total_sessions": 0, "last_session_id": "", "last_peer": "admin-web", "profile_name": fields.get("nome_meganet", ""), "bootstrap_login": "", "bootstrap_password": "", "fields": fields, "history": []}
        try:
            _write_cadastro_db(db_path, data)
        except (OSError, TypeError, ValueError) as exc:
            logging.error("ADMIN_USER_CREATE_SAVE_FAILED key=%s cadastro_db=%s error=%s", key, db_path, exc)
            return self._send(500, html_page("Falha ao criar usuário", "<p class='warn'>Não foi possível persistir o novo usuário. O cadastro não foi alterado.</p><p><a href='/users'>Voltar aos usuários</a></p>"))
        try:
            MailStore(pathlib.Path(self.app_context.args.mail_store_dir), getattr(self.app_context.args, "mail_inbox", None), mail_domain=self.app_context.args.mail_domain, admin_email=self.app_context.args.admin_email)._ensure_mailbox(fields.get("email_meganet"))
        except Exception as exc:
            logging.warning("ADMIN_USER_MAILBOX_ENSURE_FAILED key=%s error=%s", key, exc)
        return self._send(200, html_page("Usuário criado", f"<p class='ok'>Usuário {html.escape(fields['nome_meganet'])} criado.</p><p><a href='/users'>Voltar aos usuários</a></p>"))

    @_cadastro_transaction
    def _post_user_save(self, parsed, form, current_user=None) -> None:
        key = form.get("key", [""])[0]
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        data = _read_cadastro_db(db_path)
        users = data.setdefault("users", {})
        rec = users.get(key) if key else None
        if not isinstance(rec, dict):
            return self._send(404, html_page("Usuário não encontrado", "<p class='warn'>Usuário não encontrado.</p><p><a href='/users'>Voltar</a></p>"))
        fields = rec.get("fields", {}) if isinstance(rec.get("fields"), dict) else {}
        new_fields = dict(fields)
        for form_key, values in form.items():
            if not form_key.startswith("field_"):
                continue
            field_name = form_key[len("field_"):].strip()
            if not field_name:
                continue
            new_fields[field_name] = (values[0] if values else "").strip()
        nome_meganet = str(new_fields.get("nome_meganet", "")).strip()
        senha = str(new_fields.get("senha", "")).strip()
        if not nome_meganet or not senha:
            return self._send(400, html_page("Erro ao salvar", _format_user_edit_form(key, {**rec, "fields": new_fields}, self.app_context.args, error="Usuário MegaNet e senha são obrigatórios.")))
        codigo = str(new_fields.get("codigo_acesso", "")).strip()
        dup_usuario = _find_duplicate_field(users, "nome_meganet", nome_meganet, exclude_key=key)
        if dup_usuario:
            return self._send(400, html_page("Erro ao salvar", _format_user_edit_form(key, {**rec, "fields": new_fields}, self.app_context.args, error=f"Usuário MegaNet já existe no cadastro {dup_usuario}.")))
        dup_codigo = _find_duplicate_field(users, "codigo_acesso", codigo, exclude_key=key) if codigo else None
        if dup_codigo and getattr(self.app_context.args, "codigo_acesso_duplicate_action", "reject-message") != "allow":
            return self._send(400, html_page("Erro ao salvar", _format_user_edit_form(key, {**rec, "fields": new_fields}, self.app_context.args, error=f"Código de acesso já existe no usuário {dup_codigo}.")))
        email_value = str(new_fields.get("email_meganet", "")).strip()
        if email_value:
            new_fields["email_meganet"] = normalize_email_address(email_value, self.app_context.args.mail_domain)
        else:
            new_fields["email_meganet"] = meganet_email_for_name(nome_meganet, self.app_context.args.mail_domain)
        dup_email = _find_duplicate_field(users, "email_meganet", new_fields.get("email_meganet"), exclude_key=key)
        if dup_email:
            return self._send(400, html_page("Erro ao salvar", _format_user_edit_form(key, {**rec, "fields": new_fields}, self.app_context.args, error=f"E-mail MegaNet já existe no usuário {dup_email}.")))
        rec["fields"] = new_fields
        rec["profile_name"] = nome_meganet
        rec["updated_at"] = utc_now_iso()
        users[key] = rec
        try:
            _write_cadastro_db(db_path, data)
        except (OSError, TypeError, ValueError) as exc:
            logging.error("ADMIN_USER_SAVE_FAILED key=%s cadastro_db=%s error=%s", key, db_path, exc)
            return self._send(500, html_page("Falha ao salvar usuário", "<p class='warn'>Não foi possível persistir as alterações do usuário. O cadastro em disco foi mantido sem alteração.</p><p><a href='/users'>Voltar aos usuários</a></p>"))
        try:
            MailStore(pathlib.Path(self.app_context.args.mail_store_dir), getattr(self.app_context.args, "mail_inbox", None), mail_domain=self.app_context.args.mail_domain, admin_email=self.app_context.args.admin_email)._ensure_mailbox(new_fields.get("email_meganet"))
        except Exception as exc:
            logging.warning("ADMIN_USER_MAILBOX_ENSURE_FAILED key=%s error=%s", key, exc)
        return self._send(200, html_page("Usuário salvo", f"<p class='ok'>Dados do usuário {html.escape(nome_meganet)} salvos e recarregados.</p><p><a href='/users'>Voltar aos usuários</a></p>"))

    @_cadastro_transaction
    def _post_user_json_save(self, parsed, form, current_user=None) -> None:
        key = form.get("key", [""])[0]
        raw = form.get("json_body", [""])[0]
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        data = _read_cadastro_db(db_path)
        users = data.setdefault("users", {})
        if key not in users:
            return self._send(404, html_page("Usuário não encontrado", "<p class='warn'>Usuário não encontrado.</p><p><a href='/users'>Voltar</a></p>"))
        try:
            rec = json.loads(raw)
            if not isinstance(rec, dict):
                raise ValueError("O JSON do usuário precisa ser um objeto.")
        except (TypeError, ValueError) as exc:
            return self._send(400, html_page("Erro no JSON", self.app_context.user_json_form(key, users.get(key, {}), str(exc))))
        fields = rec.get("fields", {}) if isinstance(rec.get("fields"), dict) else {}
        nome_meganet = str(fields.get("nome_meganet", "")).strip()
        senha = str(fields.get("senha", "")).strip()
        if not nome_meganet or not senha:
            return self._send(400, html_page("Erro no JSON", self.app_context.user_json_form(key, rec, "fields.nome_meganet e fields.senha são obrigatórios.")))
        duplicate_name = _find_duplicate_field(users, "nome_meganet", nome_meganet, exclude_key=key)
        if duplicate_name:
            return self._send(400, html_page("Erro no JSON", self.app_context.user_json_form(key, rec, f"fields.nome_meganet já existe no usuário {duplicate_name}.")))
        codigo = str(fields.get("codigo_acesso", "")).strip()
        duplicate_code = _find_duplicate_field(users, "codigo_acesso", codigo, exclude_key=key) if codigo else None
        if duplicate_code and getattr(self.app_context.args, "codigo_acesso_duplicate_action", "reject-message") != "allow":
            return self._send(400, html_page("Erro no JSON", self.app_context.user_json_form(key, rec, f"fields.codigo_acesso já existe no usuário {duplicate_code}.")))
        email_value = str(fields.get("email_meganet", "")).strip()
        if email_value:
            fields["email_meganet"] = normalize_email_address(email_value, self.app_context.args.mail_domain)
            duplicate_email = _find_duplicate_field(users, "email_meganet", fields["email_meganet"], exclude_key=key)
            if duplicate_email:
                return self._send(400, html_page("Erro no JSON", self.app_context.user_json_form(key, rec, f"fields.email_meganet já existe no usuário {duplicate_email}.")))
        rec["updated_at"] = utc_now_iso()
        users[key] = rec
        try:
            _write_cadastro_db(db_path, data)
        except (OSError, TypeError, ValueError) as exc:
            logging.error("ADMIN_USER_JSON_SAVE_FAILED key=%s cadastro_db=%s error=%s", key, db_path, exc)
            return self._send(500, html_page("Falha ao salvar JSON", "<p class='warn'>Não foi possível persistir o JSON do usuário. O cadastro em disco foi mantido sem alteração.</p><p><a href='/users'>Voltar aos usuários</a></p>"))
        return self._send(200, html_page("JSON salvo", f"<p class='ok'>JSON do usuário {html.escape(key)} salvo.</p><p><a href='/users'>Voltar aos usuários</a></p>"))

    @_cadastro_transaction
    def _post_user_delete(self, parsed, form, current_user=None) -> None:
        key = form.get("key", [""])[0]
        db_path = pathlib.Path(self.app_context.args.cadastro_db)
        data = _read_cadastro_db(db_path)
        users = data.setdefault("users", {})
        rec = users.get(key) if key else None
        if not isinstance(rec, dict):
            return self._send(404, html_page("Usuário não encontrado", "<p class='warn'>Usuário não encontrado.</p><p><a href='/users'>Voltar</a></p>"))
        fields = rec.get("fields", {}) if isinstance(rec.get("fields"), dict) else {}
        nome = str(fields.get("nome_meganet") or key)
        email_addr = str(fields.get("email_meganet") or meganet_email_for_name(nome, getattr(self.app_context.args, "mail_domain", "meganet.tectoy.com.br")))
        del users[key]
        try:
            _write_cadastro_db(db_path, data)
        except (OSError, TypeError, ValueError) as exc:
            logging.error("ADMIN_USER_DELETE_SAVE_FAILED key=%s cadastro_db=%s error=%s", key, db_path, exc)
            return self._send(500, html_page("Falha ao excluir usuário", "<p class='warn'>Não foi possível persistir a exclusão. O cadastro em disco foi mantido sem alteração.</p><p><a href='/users'>Voltar aos usuários</a></p>"))
        logging.warning("ADMIN_USER_DELETED key=%s nome_meganet=%s email=%s cadastro_db=%s", key, nome, email_addr, db_path)
        return self._send(200, html_page("Usuário excluído", f"<p class='ok'>Usuário {html.escape(nome)} excluído do banco de cadastros.</p><p class='muted'>A caixa postal {html.escape(email_addr)} não foi apagada automaticamente.</p><p><a href='/users'>Voltar aos usuários</a></p>"))

    def _post_mail_send(self, parsed, form, current_user=None) -> None:
        to = form.get("to", [""])[0]
        subject = form.get("subject", ["MEGA NET"])[0]
        body = form.get("body", [""])[0]
        store = MailStore(pathlib.Path(self.app_context.args.mail_store_dir), getattr(self.app_context.args, "mail_inbox", None), mail_domain=self.app_context.args.mail_domain, admin_email=self.app_context.args.admin_email)
        msg = {"id": f"ADM-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}", "timestamp": utc_now_iso(), "from": self.app_context.args.admin_email, "to": normalize_email_address(to, self.app_context.args.mail_domain), "subject": subject or "MEGA NET", "body": body.splitlines() or [body or "MENSAGEM DO ADMIN"], "type": "admin"}
        store.append_to_mailbox(to, msg)
        return self._send(200, html_page("Mensagem enviada", f"<p class='ok'>Mensagem enviada para {html.escape(to)}</p><p><a href='/mailboxes'>Voltar</a></p>"))

    def _post_mailbox_clear(self, parsed, form, current_user=None) -> None:
        email_addr = form.get("email", [""])[0]
        store = MailStore(pathlib.Path(self.app_context.args.mail_store_dir), getattr(self.app_context.args, "mail_inbox", None), mail_domain=self.app_context.args.mail_domain, admin_email=self.app_context.args.admin_email)
        count, _ = store.clear_mailbox_messages(email_addr, archive=True, reason="admin-clear")
        return self._send(200, html_page("Caixa limpa", f"<p class='ok'>{count} mensagens removidas de {html.escape(email_addr)}</p><p><a href='/mailboxes'>Voltar</a></p>"))

