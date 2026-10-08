"""MegaNet-specific administrative helpers and user forms."""
from __future__ import annotations

import argparse
import html
import json
from functools import wraps
import pathlib
from typing import Optional

from ..common import utc_now_iso
from ..storage import atomic_write_json
from ..meganet.accounts import (
    CadastroDatabase, _CADASTRO_WRITE_LOCK, normalize_user_key, normalize_codigo_acesso, normalize_email_address,
    meganet_email_for_name,
)
from ..meganet.mail import MailStore

def _cadastro_transaction(func):
    """Serialize admin cadastro mutations with runtime CadastroDatabase writers."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _CADASTRO_WRITE_LOCK:
            return func(*args, **kwargs)
    return wrapped

def _read_cadastro_db(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": CadastroDatabase.SCHEMA, "users": {}}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"schema": CadastroDatabase.SCHEMA, "users": {}}
    if not isinstance(data, dict):
        data = {"schema": CadastroDatabase.SCHEMA, "users": {}}
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    data.setdefault("schema", CadastroDatabase.SCHEMA)
    data.setdefault("created_at", utc_now_iso())
    data.setdefault("updated_at", utc_now_iso())
    return data

def _write_cadastro_db(path: pathlib.Path, data: dict) -> None:
    data["updated_at"] = utc_now_iso()
    atomic_write_json(path, data, ensure_ascii=False, indent=2, sort_keys=True)

def _read_cadastro_users(path: pathlib.Path) -> dict:
    data = _read_cadastro_db(path)
    users = data.get("users", {}) if isinstance(data, dict) else {}
    return users if isinstance(users, dict) else {}

def _meganet_user_field_labels() -> dict[str, str]:
    return {
        "nome": "Nome",
        "nome_meganet": "Usuário MegaNet",
        "email_meganet": "E-mail MegaNet",
        "senha": "Senha",
        "codigo_acesso": "Código de acesso",
        "apelido": "Apelido",
        "data_nascimento": "Data de nascimento",
        "sexo": "Sexo",
        "endereco": "Endereço",
        "numero": "Número",
        "bairro": "Bairro",
        "cidade": "Cidade",
        "estado": "Estado",
        "cep": "CEP",
        "telefone_casa": "Telefone casa",
        "telefone_trabalho_pai_mae": "Tel. do trabalho do seu Pai ou Mãe",
        "telefone_trabalho_e_de_pai_ou_mae": "É do seu Pai ou Mãe?",
        "pai": "Pai",
        "mae": "Mãe",
        "irmao1_nome": "Irmão 1 - Nome",
        "irmao1_nascimento": "Irmão 1 - Data de nascimento",
        "irmao1_sexo": "Irmão 1 - Sexo",
        "irmao2_nome": "Irmão 2 - Nome",
        "irmao2_nascimento": "Irmão 2 - Data de nascimento",
        "irmao2_sexo": "Irmão 2 - Sexo",
        "irmao3_nome": "Irmão 3 - Nome",
        "irmao3_nascimento": "Irmão 3 - Data de nascimento",
        "irmao3_sexo": "Irmão 3 - Sexo",
        "videogame_tectoy": "Quais video games TEC TOY você tem?",
        "outros_videogames": "Quais outros?",
        "quantidade_cartuchos": "Quantos cartuchos você tem?",
    }

def _meganet_user_field_label(name: str) -> str:
    labels = _meganet_user_field_labels()
    if name in labels:
        return labels[name]
    return str(name).replace("_", " ").strip().capitalize() or "Campo"

def _admin_user_field_names(fields: Optional[dict] = None) -> list[str]:
    base_order = [
        "nome", "nome_meganet", "email_meganet", "senha", "codigo_acesso",
        "apelido", "data_nascimento", "sexo", "endereco", "numero",
        "bairro", "cidade", "estado", "cep", "telefone_casa",
        "telefone_trabalho_pai_mae", "telefone_trabalho_e_de_pai_ou_mae",
        "pai", "mae",
        "irmao1_nome", "irmao1_nascimento", "irmao1_sexo",
        "irmao2_nome", "irmao2_nascimento", "irmao2_sexo",
        "irmao3_nome", "irmao3_nascimento", "irmao3_sexo",
        "videogame_tectoy", "outros_videogames", "quantidade_cartuchos",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in base_order:
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    if isinstance(fields, dict):
        for name in sorted(str(k) for k in fields.keys()):
            if name and name not in seen:
                ordered.append(name)
                seen.add(name)
    return ordered

def _find_duplicate_field(users: dict, field_name: str, value: object, *, exclude_key: str = "") -> Optional[str]:
    if value in (None, ""):
        return None
    if field_name == "nome_meganet":
        wanted = normalize_user_key(value)
    elif field_name == "codigo_acesso":
        wanted = normalize_codigo_acesso(value)
    elif field_name == "email_meganet":
        wanted = normalize_email_address(value, "meganet.tectoy.com.br")
    else:
        wanted = str(value or "").strip().casefold()
    if not wanted:
        return None
    for key, rec in users.items():
        if str(key) == str(exclude_key):
            continue
        fields = rec.get("fields", {}) if isinstance(rec, dict) else {}
        current = fields.get(field_name, "") if isinstance(fields, dict) else ""
        if field_name == "nome_meganet":
            current_norm = normalize_user_key(current)
        elif field_name == "codigo_acesso":
            current_norm = normalize_codigo_acesso(current)
        elif field_name == "email_meganet":
            current_norm = normalize_email_address(current, "meganet.tectoy.com.br")
        else:
            current_norm = str(current or "").strip().casefold()
        if current_norm and current_norm == wanted:
            return str(key)
    return None

def _format_user_edit_form(key: str, rec: dict, args: argparse.Namespace, *, error: str = "") -> str:
    fields = rec.get("fields", {}) if isinstance(rec, dict) else {}
    if not isinstance(fields, dict):
        fields = {}
    required = {"nome", "nome_meganet", "senha", "codigo_acesso"}
    placeholders = {
        "data_nascimento": "DD/MM/YY",
        "irmao1_nascimento": "DD/MM/YY",
        "irmao2_nascimento": "DD/MM/YY",
        "irmao3_nascimento": "DD/MM/YY",
        "estado": "UF",
        "cep": "00000-000",
    }

    def required_mark(name: str) -> str:
        return " <span class='warn'>*</span>" if name in required else ""

    def input_row(name: str) -> str:
        value = html.escape(str(fields.get(name, "") or ""), quote=True)
        placeholder = html.escape(placeholders.get(name, ""), quote=True)
        return (
            f"<label>{html.escape(_meganet_user_field_label(name))}{required_mark(name)}"
            f"<input name='field_{html.escape(name)}' value='{value}' placeholder='{placeholder}'></label>"
        )

    field_names = _admin_user_field_names(fields)
    principal = [name for name in ["nome", "nome_meganet", "email_meganet", "senha", "codigo_acesso"] if name in field_names]
    pessoais = [name for name in ["apelido", "data_nascimento", "sexo", "endereco", "numero", "bairro", "cidade", "estado", "cep", "telefone_casa", "telefone_trabalho_pai_mae", "telefone_trabalho_e_de_pai_ou_mae", "pai", "mae"] if name in field_names]
    irmaos = [name for name in field_names if name.startswith("irmao")]
    videogames = [name for name in ["videogame_tectoy", "outros_videogames", "quantidade_cartuchos"] if name in field_names]
    extras = [name for name in field_names if name not in set(principal + pessoais + irmaos + videogames)]

    def section(title: str, names: list[str]) -> str:
        if not names:
            return ""
        return f"<h4 class='section-title'>{html.escape(title)}</h4><div class='grid'>{''.join(input_row(name) for name in names)}</div>"

    msg = f"<p class='warn'>{html.escape(error)}</p>" if error else ""
    nome_meganet = str(fields.get("nome_meganet") or "")
    nome = str(fields.get("nome") or "")
    email = str(fields.get("email_meganet") or meganet_email_for_name(nome_meganet, getattr(args, "mail_domain", "meganet.tectoy.com.br")) if nome_meganet else "")
    return f"""{msg}
    <div class='card'><h3>Editar usuário</h3>
        <p><b>Usuário MegaNet:</b> <code>{html.escape(nome_meganet or key)}</code></p>
        <p><b>Nome:</b> <code>{html.escape(nome or '-')}</code></p>
        <p><b>E-mail MegaNet:</b> <code>{html.escape(email or '-')}</code></p>
        <form method='post' action='/user/save'>
            <input type='hidden' name='key' value='{html.escape(key)}'>
            <h4>Campos principais</h4>
            <div class='grid'>{''.join(input_row(name) for name in principal)}</div>
            {section('Dados pessoais', pessoais)}
            {section('Irmãos', irmaos)}
            {section('Videogames', videogames)}
            {section('Outros campos do cadastro', extras)}
            <button type='submit' class='okbtn'>Salvar Dados e Recarregar</button>
            <a class='button secondary' href='/users'>Cancelar</a>
        </form>
    </div>
    <div class='card'>
        <h3>Excluir usuário</h3>
        <p class='warn'>A exclusão remove o usuário do banco de cadastros. A caixa postal pode permanecer em disco para auditoria.</p>
        <form method='post' action='/user/delete' onsubmit='return confirm("Excluir definitivamente o usuário {html.escape(nome_meganet or key)}?");'>
            <input type='hidden' name='key' value='{html.escape(key)}'>
            <button type='submit' style='background:#8b0000;color:white'>Excluir usuário</button>
            <a class='button secondary' href='/users'>Cancelar</a>
        </form>
    </div>"""

def _mailbox_rows(store: MailStore) -> list[tuple[str, pathlib.Path, int]]:
    rows = []
    store.mailboxes_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(store.mailboxes_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        email = str(data.get("email") or f"{path.stem}@{store.mail_domain}")
        messages = data.get("messages", [])
        rows.append((email, path, len(messages) if isinstance(messages, list) else 0))
    return rows

