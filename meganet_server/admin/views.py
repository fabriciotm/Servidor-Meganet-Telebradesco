"""Views HTML reutilizáveis da interface administrativa."""

from __future__ import annotations

import html
import json
from typing import Optional

from .auth import format_datetime, normalize_role, set_current_user


def render_login_page(error: str = "", next_url: str = "/", *, inject_csrf) -> bytes:
    msg = f"<p class='login-error'>{html.escape(error)}</p>" if error else ""
    body = f"""
    <main class='login-page'>
      <section class='login-card' aria-label='Acesso ao sistema'>
        <div class='login-brand'>
          <img src='/logo_side_brand.png' alt='Servidor Meganet - Telebradesco'>
        </div>
        <h1>Acesso ao sistema</h1>
        <p class='muted'>Informe usuário e senha para abrir a administração do Servidor Meganet - Telebradesco.</p>
        {msg}
        <form method='post' action='/login'>
          <input type='hidden' name='next' value='{html.escape(next_url, quote=True)}'>
          <label>Usuário<input name='username' autocomplete='username' autofocus></label>
          <label>Senha<input name='password' type='password' autocomplete='current-password'></label>
          <button type='submit'>Entrar</button>
        </form>
        <p class='muted small'>Usuário inicial: <code>admin</code>. Senha inicial: <code>admin</code>.</p>
        <p class='muted small'><a href='https://github.com/fabriciotm/Servidor-Meganet-Telebradesco' target='_blank' rel='noopener noreferrer'>github.com/fabriciotm/Servidor-Meganet-Telebradesco</a></p>
      </section>
    </main>"""
    body = inject_csrf(body)
    set_current_user(None)
    css = r"""
    :root{--bg:#f4f6fb;--surface:#ffffff;--line:#e5eaf2;--text:#0f172a;--muted:#6b7280;--green:#13b16f;--green2:#0c9b61;--red:#b91c1c;--shadow:0 22px 70px rgba(15,23,42,.14)}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;color:var(--text)}
    body{background:radial-gradient(circle at top left,rgba(19,177,111,.18),transparent 32%),linear-gradient(180deg,#f6f8fb 0,#eef3f9 100%)}
    .login-page{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:28px}
    .login-card{width:100%;max-width:480px;background:rgba(255,255,255,.96);border:1px solid var(--line);border-radius:28px;box-shadow:var(--shadow);padding:32px}
    .login-brand{display:flex;align-items:center;justify-content:center;margin:0 0 18px}.login-brand img{display:block;max-width:260px;width:100%;height:auto;object-fit:contain}
    h1{margin:0 0 8px;font-size:26px;letter-spacing:-.03em;text-align:center}.muted{margin:0 0 20px;color:var(--muted);line-height:1.55;text-align:center}.small{margin:18px 0 0;font-size:13px}
    label{display:block;margin:14px 0 0;font-weight:800;color:#334155;font-size:13px}input{width:100%;height:46px;margin-top:7px;padding:0 13px;border-radius:14px;border:1px solid #d8e0ea;background:#fff;color:var(--text);font-size:15px;outline:none}input:focus{border-color:#9de3c2;box-shadow:0 0 0 4px rgba(19,177,111,.14)}
    button{width:100%;height:46px;margin-top:18px;border:0;border-radius:14px;background:linear-gradient(135deg,var(--green),var(--green2));color:#fff;font-weight:900;cursor:pointer;font-size:15px}.login-error{margin:12px 0;padding:12px 14px;border-radius:14px;background:#fff1f2;border:1px solid #fecdd3;color:var(--red);font-weight:800}code{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:2px 6px;color:#0f172a}a{color:#0c9b61;text-decoration:none}a:hover{text-decoration:underline}
    @media(max-width:560px){.login-page{padding:16px}.login-card{padding:24px;border-radius:22px}.login-brand img{max-width:220px}}
    """
    doc = f"""<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Login — Servidor Meganet - Telebradesco</title><link rel='icon' href='/favicon.ico'><style>{css}</style></head><body>{body}</body></html>"""
    return doc.encode("utf-8")


def render_admin_user_management(auth_manager, *, page_renderer, notices: Optional[list[str]] = None) -> bytes:
    data = auth_manager.ensure_users()
    users = data.get("users", {}) if isinstance(data.get("users"), dict) else {}
    rows: list[str] = []
    for username, rec in sorted(users.items()):
        if not isinstance(rec, dict):
            continue
        username_text = str(username)
        username_attr = html.escape(username_text, quote=True)
        role = normalize_role(rec.get("role"))
        active = bool(rec.get("active", True))
        created = format_datetime(rec.get("created_at"))
        updated = format_datetime(rec.get("updated_at"))
        rows.append(f"""
        <tr>
          <td><label class='row-check'><input type='checkbox' name='selected' value='{username_attr}'><span><b>{html.escape(username_text)}</b></span></label></td>
          <td><select name='role__{username_attr}'><option value='admin' {'selected' if role == 'admin' else ''}>Administrador</option><option value='user' {'selected' if role != 'admin' else ''}>Usuário</option></select></td>
          <td><select name='active__{username_attr}'><option value='true' {'selected' if active else ''}>Ativo</option><option value='false' {'selected' if not active else ''}>Inativo</option></select></td>
          <td class='password-cell'><input type='password' name='password__{username_attr}' placeholder='manter atual'></td>
          <td>{html.escape(created)}</td><td>{html.escape(updated)}</td>
        </tr>""")
    notice_html = "<div class='card'>" + "".join(notices or []) + "</div>" if notices else ""
    body = f"""
    {notice_html}
    <div class='grid-compact'>
      <div class='metric'><strong>{len(users)}</strong><span>usuários do sistema</span></div>
      <div class='metric'><strong>{sum(1 for v in users.values() if isinstance(v, dict) and normalize_role(v.get('role')) == 'admin')}</strong><span>administradores</span></div>
      <div class='metric'><strong>{sum(1 for v in users.values() if isinstance(v, dict) and normalize_role(v.get('role')) != 'admin')}</strong><span>somente leitura</span></div>
    </div>
    <div class='card'>
      <div class='section-header'><div><h2>Gerência de acesso</h2><p class='muted'>Selecione um ou mais usuários para salvar alterações ou excluir. O perfil Usuário pode visualizar as informações, mas não pode alterar dados.</p></div></div>
      <form class='admin-user-form' method='post' action='/system/users/bulk' onsubmit='if(document.activeElement && document.activeElement.value==="delete") return confirm("Excluir os usuários selecionados?"); return true;'>
        <table class='admin-user-table'><thead><tr><th>Usuário</th><th>Perfil</th><th>Status</th><th>Senha nova</th><th>Criado Em</th><th>Atualizado Em</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
        <div class='admin-user-actions'><button class='okbtn' type='submit' name='action' value='save'>Salvar</button><button class='warnbtn' type='submit' name='action' value='delete'>Excluir</button><span class='muted'>Os botões atuam apenas nos usuários marcados.</span></div>
      </form>
    </div>
    <div class='card'>
      <h3>Novo usuário do sistema</h3>
      <form method='post' action='/system/users/create'>
        <div class='grid'><label>Usuário<input name='username' required></label><label>Senha<input type='password' name='password' required></label><label>Perfil<select name='role'><option value='user'>Usuário</option><option value='admin'>Administrador</option></select></label></div>
        <div class='button-row-left'><button class='okbtn' type='submit'>Criar usuário</button></div>
      </form>
    </div>
    """
    return page_renderer("Gerência", body)


def _safe_field_value(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def user_create_form(fields: Optional[dict] = None, error: str = "") -> str:
    fields = dict(fields or {})
    required = ["nome", "nome_meganet", "senha", "codigo_acesso"]
    label_map = {
        "nome": "Nome", "nome_meganet": "Nome no MEGA NET", "senha": "Senha", "codigo_acesso": "Código de Acesso",
        "apelido": "Apelido", "data_nascimento": "Data de nascimento", "sexo": "Sexo", "endereco": "Endereço",
        "numero": "Número", "bairro": "Bairro", "cidade": "Cidade", "estado": "Estado", "cep": "CEP",
        "telefone_casa": "Telefone casa", "telefone_trabalho_pai_mae": "Tel. do trabalho do seu Pai ou Mãe",
        "telefone_trabalho_e_de_pai_ou_mae": "É do seu Pai ou Mãe?", "pai": "Pai", "mae": "Mãe",
        "irmao1_nome": "Nome", "irmao1_nascimento": "Data de nascimento", "irmao1_sexo": "Sexo",
        "irmao2_nome": "Nome", "irmao2_nascimento": "Data de nascimento", "irmao2_sexo": "Sexo",
        "irmao3_nome": "Nome", "irmao3_nascimento": "Data de nascimento", "irmao3_sexo": "Sexo",
        "videogame_tectoy": "Quais video games TEC TOY você tem?", "outros_videogames": "Quais outros?",
        "quantidade_cartuchos": "Quantos cartuchos você tem?",
    }
    placeholders = {"data_nascimento": "DD/MM/YY", "irmao1_nascimento": "DD/MM/YY", "irmao2_nascimento": "DD/MM/YY", "irmao3_nascimento": "DD/MM/YY", "estado": "UF", "cep": "00000-000"}

    def required_mark(name: str) -> str:
        return " <span class='warn'>*</span>" if name in required else ""

    def help_codigo() -> str:
        msg = "O código está na etiqueta atrás do cartucho. Caso não tenha, gere um código na aba Gerador de Código de Acesso."
        return f" <button class='help-q' type='button' title='{html.escape(msg, quote=True)}' onclick=\"alert('{html.escape(msg, quote=True)}')\">?</button>"

    def input_field(name: str, label: str = "", *, kind: str = "text") -> str:
        label = label or label_map.get(name, name)
        value = _safe_field_value(fields.get(name, ""))
        placeholder = html.escape(placeholders.get(name, ""), quote=True)
        extra = help_codigo() if name == "codigo_acesso" else ""
        label_class = " class='has-help'" if name == "codigo_acesso" else ""
        return f"<label{label_class}>{html.escape(label)}{required_mark(name)}{extra}<input type='{kind}' name='field_{html.escape(name)}' value='{value}' placeholder='{placeholder}'></label>"

    def select_field(name: str, options: list[tuple[str, str]], label: str = "") -> str:
        label = label or label_map.get(name, name)
        current = str(fields.get(name, "") or "").strip().upper()
        valid_values = [value for value, _ in options]
        if current not in valid_values:
            current = valid_values[0]
        opts = [f"<option value='{html.escape(value)}' {'selected' if current == value else ''}>{html.escape(display)}</option>" for value, display in options]
        return f"<label>{html.escape(label)}{required_mark(name)}<select name='field_{html.escape(name)}'>{''.join(opts)}</select></label>"

    sex_options = [("M", "Masculino"), ("F", "Feminino")]
    parent_options = [("P", "Pai"), ("M", "Mãe")]
    msg = f"<p class='warn'>{html.escape(error)}</p>" if error else ""
    body_required = "".join([input_field("nome"), input_field("nome_meganet"), input_field("senha"), input_field("codigo_acesso")])
    body_personal = "".join([input_field("apelido"), input_field("data_nascimento"), select_field("sexo", sex_options), input_field("endereco"), input_field("numero"), input_field("bairro"), input_field("cidade"), input_field("estado"), input_field("cep"), input_field("telefone_casa"), input_field("telefone_trabalho_pai_mae"), select_field("telefone_trabalho_e_de_pai_ou_mae", parent_options)])
    body_parents = "".join([input_field("pai"), input_field("mae")])

    def sibling_card(number: int) -> str:
        prefix = f"irmao{number}"
        return f"""<div class='mini-card'><h4>Irmão {number}</h4><div class='grid'>{input_field(prefix + '_nome')}{input_field(prefix + '_nascimento')}{select_field(prefix + '_sexo', sex_options)}</div></div>"""

    body_siblings = "".join([sibling_card(1), sibling_card(2), sibling_card(3)])
    body_games = "".join([input_field("videogame_tectoy"), input_field("outros_videogames"), input_field("quantidade_cartuchos")])
    return f"""
    <div class='card'><h3>Criar usuário</h3>{msg}
      <form method='post' action='/user/create'>
        <h4>Campos obrigatórios</h4><div class='grid'>{body_required}</div>
        <h4>Campos opcionais</h4><div class='grid'>{body_personal}</div>
        <h4 class='section-title'>Nomes completos</h4><div class='grid'>{body_parents}</div>
        <h4 class='section-title'>Nome de Irmãos:</h4><div class='sibling-list'>{body_siblings}</div>
        <h4 class='section-title'>Videogames</h4><div class='grid'>{body_games}</div>
        <button class='okbtn' type='submit'>Criar usuário</button><a class='button secondary' href='/users'>Cancelar</a>
      </form>
    </div>"""


def user_json_form(key: str, rec: dict, error: str = "") -> str:
    msg = f"<p class='warn'>{html.escape(error)}</p>" if error else ""
    raw = json.dumps(rec, ensure_ascii=False, indent=2, sort_keys=True)
    return f"""
    <div class='card'><h3>Editar JSON do usuário</h3>{msg}
      <p class='muted'>Edição avançada. Um JSON inválido ou campos inconsistentes podem impedir login, cadastro ou correio do usuário.</p>
      <form method='post' action='/user/json/save'>
        <input type='hidden' name='key' value='{html.escape(key)}'>
        <label>JSON do usuário<textarea class='raw-json' name='json_body'>{html.escape(raw)}</textarea></label>
        <button class='okbtn' type='submit'>Salvar JSON</button><a class='button secondary' href='/users'>Cancelar</a>
      </form>
    </div>"""
