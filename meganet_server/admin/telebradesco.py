from __future__ import annotations

import html
import json
import pathlib
import re
import urllib.parse
from typing import Optional

from ..common import utc_now_iso
from ..telebradesco.content import (
    TELEBRADESCO_CARD_IDENTIFIER_FIELD, blank_telebradesco_content,
    normalize_telebradesco_content, validate_telebradesco_content,
)
from .auth import format_datetime as _format_admin_datetime

def _tele_form_first(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key, [])
    return str(values[0] if values else default).strip()
def _tele_form_rows(form: dict[str, list[str]], mapping: list[tuple[str, str]]) -> list[dict]:
    columns = {
        field_name: [str(value).strip() for value in form.get(input_name, [])]
        for field_name, input_name in mapping
    }
    row_count = max((len(values) for values in columns.values()), default=0)
    rows: list[dict] = []
    for index in range(row_count):
        row = {
            field_name: values[index] if index < len(values) else ""
            for field_name, values in columns.items()
        }
        if any(str(value).strip() for value in row.values()):
            rows.append(row)
    return rows
def _tele_cards_from_form(form: dict[str, list[str]]) -> list[dict]:
    """Read indexed card blocks from the administrative form."""
    indices: set[int] = set()
    for key in form:
        match = re.fullmatch(r"cartao_(\d+)_(?:data_cartao|bandeira|final|compra_.+)", str(key))
        if match:
            indices.add(int(match.group(1)))

    cards: list[dict] = []
    for index in sorted(indices):
        prefix = f"cartao_{index}"
        card = {
            TELEBRADESCO_CARD_IDENTIFIER_FIELD: _tele_form_first(form, prefix + "_data_cartao"),
            "bandeira": _tele_form_first(form, prefix + "_bandeira").upper(),
            "final": re.sub(r"\D", "", _tele_form_first(form, prefix + "_final"))[-4:],
            "compras": _tele_form_rows(form, [
                ("data", prefix + "_compra_data"),
                ("descricao", prefix + "_compra_descricao"),
                ("valor", prefix + "_compra_valor"),
            ]),
        }
        if any(
            str(card.get(key) or "").strip()
            for key in (TELEBRADESCO_CARD_IDENTIFIER_FIELD, "bandeira", "final")
        ) or card["compras"]:
            cards.append(card)
    return cards
def telebradesco_content_from_form(form: dict[str, list[str]]) -> dict:
    content = {
        "schema": "telebradesco_content_v5",
        "updated_at": utc_now_iso(),
        "conta_corrente": {
            "titulo": _tele_form_first(form, "cc_titulo"),
            "texto_auxiliar": _tele_form_first(form, "cc_texto_auxiliar"),
            "data_saldo": _tele_form_first(form, "cc_data_saldo"),
            "saldo": _tele_form_first(form, "cc_saldo"),
            "saldo_disponivel": _tele_form_first(form, "cc_saldo_disponivel"),
            "saldo_vinculado": _tele_form_first(form, "cc_saldo_vinculado"),
            "saldo_total": _tele_form_first(form, "cc_saldo_total"),
            "extrato": _tele_form_rows(form, [
                ("data", "cc_extrato_data"),
                ("historico", "cc_extrato_historico"),
                ("documento", "cc_extrato_documento"),
                ("valor", "cc_extrato_valor"),
            ]),
            "ultimos_lancamentos": _tele_form_rows(form, [
                ("data", "cc_ultimos_data"),
                ("valor", "cc_ultimos_valor"),
            ])[:3],
            "lancamentos_futuros": _tele_form_rows(form, [
                ("data", "cc_futuros_data"),
                ("historico", "cc_futuros_historico"),
                ("documento", "cc_futuros_documento"),
                ("valor", "cc_futuros_valor"),
            ]),
        },
        "investimentos": _tele_form_rows(form, [
            ("descricao", "investimento_descricao"),
            ("valor", "investimento_valor"),
        ]),
        "poupanca": {
            "titulo": _tele_form_first(form, "poup_titulo"),
            "texto_auxiliar": _tele_form_first(form, "poup_texto_auxiliar"),
            "data_saldo": _tele_form_first(form, "poup_data_saldo"),
            "saldo": _tele_form_first(form, "poup_saldo"),
            "saldo_disponivel": _tele_form_first(form, "poup_saldo_disponivel"),
            "saldo_vinculado": _tele_form_first(form, "poup_saldo_vinculado"),
            "saldo_total": _tele_form_first(form, "poup_saldo_total"),
            "extrato_total": _tele_form_rows(form, [
                ("data", "poup_extrato_data"),
                ("historico", "poup_extrato_historico"),
                ("documento", "poup_extrato_documento"),
                ("valor", "poup_extrato_valor"),
            ]),
            "ultimos_lancamentos": _tele_form_rows(form, [
                ("data", "poup_ultimos_data"),
                ("historico", "poup_ultimos_historico"),
                ("documento", "poup_ultimos_documento"),
                ("valor", "poup_ultimos_valor"),
            ]),
            "resumo": _tele_form_rows(form, [
                ("data", "poup_resumo_data"),
                ("valor", "poup_resumo_valor"),
            ])[:3],
        },
        "cartoes": _tele_cards_from_form(form),
    }
    return validate_telebradesco_content(content)
def _tele_h(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
def _tele_input(
    name: str,
    value: object,
    *,
    maxlength: int,
    placeholder: str = "",
    inputmode: str = "",
    input_type: str = "text",
    autocomplete: str = "off",
) -> str:
    mode = f" inputmode='{_tele_h(inputmode)}'" if inputmode else ""
    return (
        f"<input type='{_tele_h(input_type)}' name='{_tele_h(name)}' value='{_tele_h(value)}' "
        f"maxlength='{int(maxlength)}' placeholder='{_tele_h(placeholder)}' "
        f"autocomplete='{_tele_h(autocomplete)}'{mode}>"
    )
def _tele_remove_button() -> str:
    return "<button type='button' class='danger tele-remove-row'>Remover</button>"
def _tele_transaction_rows(records: object, prefix: str) -> str:
    rows = records if isinstance(records, list) and records else [{}]
    rendered = []
    for item in rows:
        item = item if isinstance(item, dict) else {}
        rendered.append(
            "<tr>"
            f"<td>{_tele_input(prefix + '_data', item.get('data'), maxlength=5, placeholder='DD/MM')}</td>"
            f"<td>{_tele_input(prefix + '_historico', item.get('historico') or item.get('descricao'), maxlength=15)}</td>"
            f"<td>{_tele_input(prefix + '_documento', item.get('documento'), maxlength=7)}</td>"
            f"<td>{_tele_input(prefix + '_valor', item.get('valor'), maxlength=19)}</td>"
            f"<td>{_tele_remove_button()}</td>"
            "</tr>"
        )
    return "".join(rendered)
def _tele_summary_rows(records: object, prefix: str) -> str:
    rows = records if isinstance(records, list) else []
    rows = list(rows[:3])
    while len(rows) < 3:
        rows.append({})
    rendered = []
    for item in rows:
        item = item if isinstance(item, dict) else {}
        rendered.append(
            "<tr>"
            f"<td>{_tele_input(prefix + '_data', item.get('data'), maxlength=8, placeholder='DD/MM/AA')}</td>"
            f"<td>{_tele_input(prefix + '_valor', item.get('valor'), maxlength=20)}</td>"
            "</tr>"
        )
    return "".join(rendered)
def _tele_current_account_last_rows(records: object) -> str:
    """Render each fixed balance as a vertical date/label/value group.

    The cartridge screen associates each date with the balance shown directly
    below it. Keeping each pair in a self-contained card prevents the value
    from appearing as an unrelated column in the administrative interface.
    """
    labels = ["Saldo Total", "Saldo Vinculado", "Saldo C/L Programado"]
    rows = records if isinstance(records, list) else []
    rows = list(rows[:3])
    while len(rows) < 3:
        rows.append({})
    rendered = []
    for index, (label, item) in enumerate(zip(labels, rows), start=1):
        item = item if isinstance(item, dict) else {}
        escaped_label = html.escape(label)
        rendered.append(
            "<section class='tele-last-card'>"
            f"<span class='tele-last-card-number'>Campo {index} de 3</span>"
            "<label class='tele-last-date-row'>"
            "<span>Data</span>"
            f"{_tele_input('cc_ultimos_data', item.get('data'), maxlength=8, placeholder='DD/MM/AA')}"
            "</label>"
            f"<div class='tele-last-balance-label'>{escaped_label}</div>"
            "<label class='tele-last-value'>"
            f"<span class='sr-only'>Valor de {escaped_label}</span>"
            f"{_tele_input('cc_ultimos_valor', item.get('valor'), maxlength=14)}"
            "</label>"
            "</section>"
        )
    return "".join(rendered)
def _tele_investment_rows(records: object) -> str:
    rows = records if isinstance(records, list) and records else [{}]
    return "".join(
        "<tr>"
        f"<td>{_tele_input('investimento_descricao', (item if isinstance(item, dict) else {}).get('descricao'), maxlength=15)}</td>"
        f"<td>{_tele_input('investimento_valor', (item if isinstance(item, dict) else {}).get('valor'), maxlength=18)}</td>"
        f"<td>{_tele_remove_button()}</td>"
        "</tr>"
        for item in rows
    )
def _tele_card_purchase_rows(records: object, card_index: object) -> str:
    rows = records if isinstance(records, list) and records else [{}]
    prefix = f"cartao_{card_index}"
    return "".join(
        "<tr>"
        f"<td>{_tele_input(prefix + '_compra_data', (item if isinstance(item, dict) else {}).get('data'), maxlength=8, placeholder='DD/MM/AA')}</td>"
        f"<td>{_tele_input(prefix + '_compra_descricao', (item if isinstance(item, dict) else {}).get('descricao'), maxlength=28)}</td>"
        f"<td>{_tele_input(prefix + '_compra_valor', (item if isinstance(item, dict) else {}).get('valor'), maxlength=20)}</td>"
        f"<td>{_tele_remove_button()}</td>"
        "</tr>"
        for item in rows
    )
def _tele_card_block(card: object, card_index: object) -> str:
    item = card if isinstance(card, dict) else {}
    prefix = f"cartao_{card_index}"
    tbody_id = f"tele_cartao_{card_index}_rows"
    template_id = f"tele_cartao_{card_index}_template"
    identifier = item.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD, "")
    return f"""
    <section class='card tele-card-block' data-card-index='{_tele_h(card_index)}'>
      <div class='section-header'>
        <div><h4>Cartão</h4><p class='muted'>Somente será enviado quando Data Cartão coincidir com os quatro últimos dígitos DDMM do login.</p></div>
        <button type='button' class='danger tele-remove-card'>Remover cartão</button>
      </div>
      <div class='grid'>
        <label>Data Cartão (DDMM), 4 dígitos{_tele_input(prefix + '_data_cartao', identifier, maxlength=4, placeholder='0103', inputmode='numeric')}</label>
        <label>Bandeira, até 21 caracteres{_tele_input(prefix + '_bandeira', item.get('bandeira'), maxlength=21)}</label>
        <label>Final do cartão{_tele_input(prefix + '_final', item.get('final'), maxlength=4, placeholder='1234', inputmode='numeric')}</label>
      </div>
      <p><b>Prévia do título:</b> <code class='tele-card-title-preview'></code></p>
      <div style='overflow-x:auto'>
        <table>
          <thead><tr><th>Data da compra</th><th>Identificação da compra</th><th>Valor</th><th></th></tr></thead>
          <tbody id='{tbody_id}'>{_tele_card_purchase_rows(item.get('compras', []), card_index)}</tbody>
        </table>
      </div>
      <button type='button' class='secondary tele-add-row' data-target='{tbody_id}' data-template='{template_id}'>Adicionar compra</button>
      <template id='{template_id}'>
        <tr>
          <td>{_tele_input(prefix + '_compra_data', '', maxlength=8, placeholder='DD/MM/AA')}</td>
          <td>{_tele_input(prefix + '_compra_descricao', '', maxlength=28)}</td>
          <td>{_tele_input(prefix + '_compra_valor', '', maxlength=20)}</td>
          <td>{_tele_remove_button()}</td>
        </tr>
      </template>
    </section>
    """
def _tele_card_blocks(cards: object) -> str:
    rows = cards if isinstance(cards, list) and cards else [{}]
    return "".join(_tele_card_block(card, index) for index, card in enumerate(rows))
def _tele_transaction_table(title: str, records: object, prefix: str, tbody_id: str, template_id: str) -> str:
    return f"""
    <h4>{html.escape(title)}</h4>
    <p class='help'>Data em DD/MM. Limites: histórico 15, documento 7 e valor 19 caracteres.</p>
    <div style='overflow-x:auto'>
      <table>
        <thead><tr><th>Data</th><th>Histórico</th><th>Documento</th><th>Valor</th><th></th></tr></thead>
        <tbody id='{tbody_id}'>{_tele_transaction_rows(records, prefix)}</tbody>
      </table>
    </div>
    <button type='button' class='secondary tele-add-row' data-target='{tbody_id}' data-template='{template_id}'>Adicionar lançamento</button>
    <template id='{template_id}'>
      <tr>
        <td>{_tele_input(prefix + '_data', '', maxlength=5, placeholder='DD/MM')}</td>
        <td>{_tele_input(prefix + '_historico', '', maxlength=15)}</td>
        <td>{_tele_input(prefix + '_documento', '', maxlength=7)}</td>
        <td>{_tele_input(prefix + '_valor', '', maxlength=19)}</td>
        <td>{_tele_remove_button()}</td>
      </tr>
    </template>
    """
def render_telebradesco_accounts_index(
    accounts: dict,
    *,
    accounts_path: pathlib.Path,
    accounts_content_path: Optional[pathlib.Path] = None,
    active_service: object,
    notices: list[str],
) -> str:
    accounts = accounts if isinstance(accounts, dict) else {}
    rows: list[str] = []
    mobile_cards: list[str] = []
    blocked_count = 0
    for key, item in sorted(accounts.items()):
        if not isinstance(item, dict):
            continue
        blocked = bool(item.get("bloqueada", False))
        blocked_count += int(blocked)
        status_class = "blocked" if blocked else ""
        status_text = "Bloqueada" if blocked else "Ativa"
        try:
            access_total = int(item.get("total_accesses") or item.get("total_sessions") or 0)
        except (TypeError, ValueError):
            access_total = 0
        last_access = _format_admin_datetime(item.get("last_access_at"))
        quoted = urllib.parse.quote(str(key))
        next_block_value = "false" if blocked else "true"
        block_label = "Desbloquear Conta" if blocked else "Bloquear Conta"
        block_button_class = "button secondary" if blocked else "button warnbtn"
        block_form = (
            f"<form class='inline-action-form' method='post' action='/telebradesco/block' "
            f"onsubmit='return confirm(\"{html.escape(block_label)}?\");'>"
            f"<input type='hidden' name='account_key' value='{html.escape(str(key), quote=True)}'>"
            f"<input type='hidden' name='blocked' value='{next_block_value}'>"
            f"<button class='{block_button_class}' type='submit'>{html.escape(block_label)}</button></form>"
        )
        name = html.escape(str(item.get("nome") or key))
        agency = html.escape(str(item.get("agencia") or ""))
        account_number = html.escape(str(item.get("conta") or ""))
        digit = html.escape(str(item.get("digito") or ""))
        rows.append(
            "<tr>"
            f"<td><a class='account-link' href='/telebradesco?account={quoted}'>{name}</a></td>"
            f"<td>{agency}</td>"
            f"<td>{account_number}-{digit}</td>"
            f"<td><span class='status-dot {status_class}'></span>{html.escape(status_text)}</td>"
            f"<td>{access_total}<br><small>{html.escape(last_access or '-')}</small></td>"
            f"<td><div class='action-row'><a class='button secondary' href='/telebradesco?account={quoted}'>Abrir conta</a>{block_form}</div></td>"
            "</tr>"
        )
        mobile_cards.append(
            "<article class='account-mobile-card'>"
            f"<h3><a class='account-link' href='/telebradesco?account={quoted}'>{name}</a></h3>"
            f"<div><span class='status-dot {status_class}'></span>{html.escape(status_text)}</div>"
            "<div class='account-mobile-meta'>"
            f"<div><span>Agência</span><b>{agency}</b></div>"
            f"<div><span>Conta</span><b>{account_number}-{digit}</b></div>"
            f"<div><span>Acessos</span><b>{access_total}</b></div>"
            "</div>"
            f"<div class='action-row'><a class='button secondary' href='/telebradesco?account={quoted}'>Abrir conta</a>{block_form}</div>"
            "</article>"
        )
    total = len(rows)
    total_accesses = 0
    for item in accounts.values():
        if isinstance(item, dict):
            try:
                total_accesses += int(item.get("total_accesses") or item.get("total_sessions") or 0)
            except (TypeError, ValueError):
                pass
    active_count = total - blocked_count
    notice_html = "<div class='card'>" + "".join(notices) + "</div>" if notices else ""
    if rows:
        listing = (
            "<div class='desktop-only'><table><thead><tr><th>Identificação</th><th>Agência</th><th>Conta</th><th>Status</th><th>Acessos</th><th>Ação</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            f"<div class='mobile-only account-mobile-list'>{''.join(mobile_cards)}</div>"
        )
    else:
        listing = """
        <div class='empty-state'>
          <h3>Nenhuma conta cadastrada</h3>
          <p>O Telebradesco consulta somente contas previamente cadastradas. Crie a primeira conta para disponibilizar autenticação e conteúdo ao cartucho.</p>
          <a class='button okbtn' href='/telebradesco?new=1'>Cadastrar primeira conta</a>
        </div>"""
    content_path_display = pathlib.Path(accounts_content_path or accounts_path.with_name('accounts_content.json'))
    return f"""
    {notice_html}
    <div class='card'><p class='muted'><b>Identificação:</b> <code>{html.escape(str(accounts_path))}</code><br><b>Conteúdo financeiro:</b> <code>{html.escape(str(content_path_display))}</code></p></div>
    <div class='grid-compact'>
      <div class='metric'><strong>{total}</strong><span>contas cadastradas</span></div>
      <div class='metric'><strong>{active_count}</strong><span>contas ativas</span></div>
      <div class='metric'><strong>{blocked_count}</strong><span>contas bloqueadas</span></div>
      <div class='metric'><strong>{total_accesses}</strong><span>acessos registrados</span></div>
    </div>
    <div class='card'>
      <div class='section-header'>
        <div><h2>Contas cadastradas</h2><p class='muted'>A identificação é lida de accounts.json; saldos, extratos, investimentos, poupança e cartões são lidos de accounts_content.json.</p></div>
        <a class='button okbtn' href='/telebradesco?new=1'>Nova conta</a>
      </div>
      {listing}
    </div>"""
def render_telebradesco_editor(
    content: dict,
    compiled: dict,
    *,
    tele_path: pathlib.Path,
    accounts_path: pathlib.Path,
    active_service: object,
    notices: list[str],
    account_key: str,
    account: dict,
    is_new: bool = False,
) -> str:
    content = blank_telebradesco_content() if is_new else normalize_telebradesco_content(content)
    cc = content["conta_corrente"]
    savings = content["poupanca"]
    cards = content.get("cartoes", [])
    investments = content.get("investimentos", [])
    raw = json.dumps(content, ensure_ascii=False, indent=2)
    account = account if isinstance(account, dict) else {}
    account_name = str(account.get("nome") or "")
    account_agencia = str(account.get("agencia") or "")
    account_conta = str(account.get("conta") or "")
    account_digito = str(account.get("digito") or "")
    account_senha = str(account.get("senha") or "")
    account_blocked = bool(account.get("bloqueada", False))
    try:
        account_accesses = int(account.get("total_accesses") or account.get("total_sessions") or 0)
    except (TypeError, ValueError):
        account_accesses = 0
    account_last_access = _format_admin_datetime(account.get("last_access_at"))

    notice_html = "<div class='card'>" + "".join(notices) + "</div>" if notices else ""
    section_rows = "".join(
        f"<tr><td>{html.escape(str(name))}</td><td>{size}</td></tr>"
        for name, size in compiled["section_sizes"].items()
    )

    generator_button = "<button type='button' class='button secondary tele-generate-account-data'>Gerar Dados</button>" if is_new else ""
    delete_account_form = "" if is_new else (
        f"<form method='post' action='/telebradesco/delete' id='tele_delete_form' onsubmit='return confirm(\"Excluir esta conta Telebradesco?\");' style='display:none'>"
        f"<input type='hidden' name='account_key' value='{html.escape(account_key)}'></form>"
    )
    clear_account_form = "" if is_new else (
        f"<form method='post' action='/telebradesco/clear-data' id='tele_clear_data_form' onsubmit='return confirm(\"Limpar todos os dados financeiros e de conteúdo desta conta?\");' style='display:none'>"
        f"<input type='hidden' name='account_key' value='{html.escape(account_key)}'></form>"
    )
    delete_account_button = "" if is_new else "<button class='warnbtn' type='submit' form='tele_delete_form'>Excluir conta</button>"
    clear_account_button = "" if is_new else "<button class='secondary' type='submit' form='tele_clear_data_form'>Limpar dados da conta</button>"
    account_action_buttons = (
        "<div class='card toolbar tele-account-actions'>"
        "<button class='okbtn' type='submit'>Salvar identificação e conteúdo</button>"
        f"{clear_account_button}"
        f"{delete_account_button}"
        "<span class='muted'>As conexões Telebradesco ativas serão encerradas para aplicar os dados novos.</span>"
        "</div>"
    )

    counts = {
        "cc_extrato": len(cc.get("extrato", []) if isinstance(cc.get("extrato"), list) else []),
        "cc_futuros": len(cc.get("lancamentos_futuros", []) if isinstance(cc.get("lancamentos_futuros"), list) else []),
        "investimentos": len(investments if isinstance(investments, list) else []),
        "poup_extrato": len(savings.get("extrato_total", []) if isinstance(savings.get("extrato_total"), list) else []),
        "poup_ultimos": len(savings.get("ultimos_lancamentos", []) if isinstance(savings.get("ultimos_lancamentos"), list) else []),
        "cartoes": len(cards if isinstance(cards, list) else []),
        "compras_cartao": sum(
            len(card.get("compras", []))
            for card in (cards if isinstance(cards, list) else [])
            if isinstance(card, dict) and isinstance(card.get("compras"), list)
        ),
    }

    cc_tables = (
        _tele_transaction_table(
            "Extrato", cc.get("extrato", []), "cc_extrato",
            "tele_cc_extrato_rows", "tele_cc_extrato_template",
        )
        + _tele_transaction_table(
            "Lançamentos Futuros", cc.get("lancamentos_futuros", []), "cc_futuros",
            "tele_cc_futuros_rows", "tele_cc_futuros_template",
        )
    )

    savings_tables = (
        _tele_transaction_table(
            "Extrato Total", savings.get("extrato_total", []), "poup_extrato",
            "tele_poup_extrato_rows", "tele_poup_extrato_template",
        )
        + _tele_transaction_table(
            "Últimos Lançamentos", savings.get("ultimos_lancamentos", []), "poup_ultimos",
            "tele_poup_ultimos_rows", "tele_poup_ultimos_template",
        )
    )

    body = f"""
    {notice_html}
    <div class='breadcrumb'><a href='/telebradesco'>Contas Telebradesco</a><span>›</span><b>{'Nova conta' if is_new else html.escape(account_name or account_key)}</b></div>
    <div class='section-header'>
      <div><h2>{'Cadastrar nova conta' if is_new else html.escape(account_name or account_key)}</h2><p class='muted'>{'Preencha autenticação e conteúdo antes de salvar.' if is_new else 'Edite autenticação e conteúdo desta conta.'}</p></div>
      <a class='button secondary' href='/telebradesco'>Voltar para contas</a>
    </div>
    <div class='grid-compact'>
      <div class='metric'><strong>{counts['cc_extrato']}</strong><span>extratos de conta corrente</span></div>
      <div class='metric'><strong>{counts['cc_futuros']}</strong><span>lançamentos futuros</span></div>
      <div class='metric'><strong>{counts['investimentos']}</strong><span>investimentos</span></div>
      <div class='metric'><strong>{counts['poup_extrato'] + counts['poup_ultimos']}</strong><span>lançamentos de poupança</span></div>
      <div class='metric'><strong>{counts['cartoes']}</strong><span>cartões cadastrados</span></div>
      <div class='metric'><strong>{counts['compras_cartao']}</strong><span>compras de cartão</span></div>
      <div class='metric'><strong>{account_accesses}</strong><span>acessos efetuados</span><small>{html.escape(account_last_access or '-')}</small></div>
    </div>

    {delete_account_form}
    {clear_account_form}
    <form method='post' action='/telebradesco/save' id='telebradesco_form'>
      <input type='hidden' name='original_account_key' value='{html.escape(account_key)}'>
      {account_action_buttons}
      <div class='card'>
        <div class='section-header tele-auth-header'><div><h3>{'Nova conta' if is_new else 'Dados de autenticação da conta'}</h3></div>{generator_button}</div>
        <p class='help'>O cartucho transmite 8 dígitos após a senha. O log real confirmou que os 4 últimos são Data Cartão em DDMM; <code>0000</code> significa que nenhum cartão foi informado. Os 4 anteriores são mantidos separadamente para diagnóstico.</p>
        <div class='grid'>
          <label>Identificação da conta{_tele_input('tele_account_name', account_name, maxlength=60)}</label>
          <label>Agência{_tele_input('tele_agencia', account_agencia, maxlength=4, placeholder='2924', inputmode='numeric')}</label>
          <label>Conta corrente{_tele_input('tele_conta', account_conta, maxlength=7, placeholder='0023456', inputmode='numeric')}</label>
          <label>Dígito{_tele_input('tele_digito', account_digito, maxlength=1, placeholder='7', inputmode='numeric')}</label>
          <label>Senha{_tele_input('tele_senha', account_senha, maxlength=4, placeholder='Deixe vazio para SENHA NAO GRAVADA', inputmode='numeric', input_type='password', autocomplete='new-password')}<span class='muted'>Opcional. Vazia: o cartucho recebe o código 010 e exibe SENHA NAO GRAVADA.</span></label>
        </div>
        <div class='inline'><label><input type='checkbox' name='tele_bloqueada' value='true' {'checked' if account_blocked else ''}> Conta bloqueada</label></div>
        <p class='muted'>O bloqueio é aplicado conforme o modo global do Telebradesco. Com a ROM original, bloqueio estrito impede o login usando o código 010 e exibe SENHA NAO GRAVADA; modo nativo 020 mantém o tratamento por operação.</p>
      </div>

      <details class='menu-group' open>
        <summary>Conta Corrente</summary>
        <div class='card'>
          <div class='grid'>
            <label>Título, até 32 caracteres{_tele_input('cc_titulo', cc.get('titulo'), maxlength=32)}</label>
            <label>Texto auxiliar, até 28 caracteres{_tele_input('cc_texto_auxiliar', cc.get('texto_auxiliar'), maxlength=28)}</label>
            <label>Data do saldo{_tele_input('cc_data_saldo', cc.get('data_saldo'), maxlength=8, placeholder='DD/MM/AA')}</label>
            <label>Saldo{_tele_input('cc_saldo', cc.get('saldo'), maxlength=20)}</label>
            <label>Saldo disponível{_tele_input('cc_saldo_disponivel', cc.get('saldo_disponivel'), maxlength=20)}</label>
            <label>Saldo vinculado{_tele_input('cc_saldo_vinculado', cc.get('saldo_vinculado'), maxlength=18)}</label>
            <label>Saldo total{_tele_input('cc_saldo_total', cc.get('saldo_total'), maxlength=20)}</label>
          </div>
          {cc_tables}
          <h4>Últimos Lançamentos</h4>
          <p class='help'>Até três pares de data e valor. O valor aceita até 14 caracteres e é alinhado à direita na mesma faixa visual da data.</p>
          <div class='tele-last-grid'>
            {_tele_current_account_last_rows(cc.get('ultimos_lancamentos', []))}
          </div>
        </div>
      </details>

      <details class='menu-group' open>
        <summary>Investimentos</summary>
        <div class='card'>
          <p class='help'>Três investimentos por página. Descrição com até 15 caracteres e valor com até 18.</p>
          <div style='overflow-x:auto'>
            <table>
              <thead><tr><th>Descrição</th><th>Valor</th><th></th></tr></thead>
              <tbody id='tele_investimentos_rows'>{_tele_investment_rows(investments)}</tbody>
            </table>
          </div>
          <button type='button' class='secondary tele-add-row' data-target='tele_investimentos_rows' data-template='tele_investimentos_template'>Adicionar investimento</button>
          <template id='tele_investimentos_template'>
            <tr>
              <td>{_tele_input('investimento_descricao', '', maxlength=15)}</td>
              <td>{_tele_input('investimento_valor', '', maxlength=18)}</td>
              <td>{_tele_remove_button()}</td>
            </tr>
          </template>
        </div>
      </details>

      <details class='menu-group' open>
        <summary>Poupança</summary>
        <div class='card'>
          <div class='grid'>
            <label>Título, até 32 caracteres{_tele_input('poup_titulo', savings.get('titulo'), maxlength=32)}</label>
            <label>Texto auxiliar, até 28 caracteres{_tele_input('poup_texto_auxiliar', savings.get('texto_auxiliar'), maxlength=28)}</label>
            <label>Data do saldo{_tele_input('poup_data_saldo', savings.get('data_saldo'), maxlength=8, placeholder='DD/MM/AA')}</label>
            <label>Saldo{_tele_input('poup_saldo', savings.get('saldo'), maxlength=20)}</label>
            <label>Saldo disponível{_tele_input('poup_saldo_disponivel', savings.get('saldo_disponivel'), maxlength=20)}</label>
            <label>Saldo vinculado{_tele_input('poup_saldo_vinculado', savings.get('saldo_vinculado'), maxlength=18)}</label>
            <label>Saldo total{_tele_input('poup_saldo_total', savings.get('saldo_total'), maxlength=20)}</label>
          </div>
          {savings_tables}
          <h4>Resumo</h4>
          <p class='help'>São aceitas até três linhas. Data em DD/MM/AA.</p>
          <table>
            <thead><tr><th>Data</th><th>Valor</th></tr></thead>
            <tbody>{_tele_summary_rows(savings.get('resumo', []), 'poup_resumo')}</tbody>
          </table>
        </div>
      </details>

      <details class='menu-group' open>
        <summary>Cartões de Crédito</summary>
        <div class='card'>
          <p class='help'><b>Data Cartão é o identificador recebido no login.</b> A seção 6 somente é enviada quando os quatro últimos dígitos DDMM do login coincidem exatamente com um cartão desta conta. <code>0000</code>, ausência ou valor não cadastrado não enviam dados de cartão.</p>
          <p class='help'>Uma conta pode possuir vários cartões, desde que cada um tenha uma Data Cartão diferente. A data de cada compra permanece em DD/MM/AA.</p>
          <div id='tele_cards_container'>{_tele_card_blocks(cards)}</div>
          <button type='button' class='secondary tele-add-card'>Adicionar cartão</button>
          <template id='tele_card_block_template'>{_tele_card_block({}, '__INDEX__')}</template>
        </div>
      </details>

      {account_action_buttons}
    </form>


    <script>
    (function(){{
      function updateCardTitle(block) {{
        if (!block) return;
        var brand = block.querySelector("[name$='_bandeira']");
        var finalDigits = block.querySelector("[name$='_final']");
        var preview = block.querySelector('.tele-card-title-preview');
        if (!preview) return;
        var b = (brand && brand.value ? brand.value : 'BANDEIRA').toUpperCase().trim();
        var f = (finalDigits && finalDigits.value ? finalDigits.value : '0000').replace(/\\D/g, '').slice(-4).padStart(4, '0');
        preview.textContent = b + ' FINAL ' + f;
      }}

      function updateAllCardTitles(root) {{
        (root || document).querySelectorAll('.tele-card-block').forEach(updateCardTitle);
      }}

      document.addEventListener('click', function(event) {{
        var generateAccount = event.target.closest('.tele-generate-account-data');
        if (generateAccount) {{
          function randint(min, max) {{ return Math.floor(Math.random() * (max - min + 1)) + min; }}
          function accountDigit(accountText) {{
            var base = String(accountText || '').replace(/\\D/g, '').padStart(8, '0').slice(-8);
            var weights = [3, 2, 7, 6, 5, 4, 3, 2];
            var sum = 0;
            for (var i = 0; i < 8; i += 1) {{ sum += parseInt(base.charAt(i), 10) * weights[i]; }}
            var rest = sum % 11;
            return rest <= 1 ? '0' : String(11 - rest);
          }}
          var agencia = String(randint(100, 3999));
          var conta = String(randint(0, 9999999)).padStart(7, '0');
          var digito = accountDigit(conta);
          var agenciaInput = document.querySelector('[name="tele_agencia"]');
          var contaInput = document.querySelector('[name="tele_conta"]');
          var digitoInput = document.querySelector('[name="tele_digito"]');
          if (agenciaInput) agenciaInput.value = agencia;
          if (contaInput) contaInput.value = conta;
          if (digitoInput) digitoInput.value = digito;
          return;
        }}

        var removeRow = event.target.closest('.tele-remove-row');
        if (removeRow) {{
          var row = removeRow.closest('tr');
          if (row) row.remove();
          return;
        }}

        var addRow = event.target.closest('.tele-add-row');
        if (addRow) {{
          var target = document.getElementById(addRow.dataset.target);
          var template = document.getElementById(addRow.dataset.template);
          if (!target || !template) return;
          target.appendChild(template.content.cloneNode(true));
          return;
        }}

        var removeCard = event.target.closest('.tele-remove-card');
        if (removeCard) {{
          var block = removeCard.closest('.tele-card-block');
          if (block) block.remove();
          return;
        }}

        var addCard = event.target.closest('.tele-add-card');
        if (addCard) {{
          var container = document.getElementById('tele_cards_container');
          var template = document.getElementById('tele_card_block_template');
          if (!container || !template) return;
          var existing = Array.from(container.querySelectorAll('.tele-card-block')).map(function(block) {{
            return parseInt(block.dataset.cardIndex || '-1', 10);
          }});
          var nextIndex = existing.length ? Math.max.apply(null, existing) + 1 : 0;
          var htmlText = template.innerHTML.split('__INDEX__').join(String(nextIndex));
          container.insertAdjacentHTML('beforeend', htmlText);
          updateAllCardTitles(container);
        }}
      }});

      document.addEventListener('input', function(event) {{
        if (event.target.matches("[name$='_bandeira'],[name$='_final']")) {{
          updateCardTitle(event.target.closest('.tele-card-block'));
        }}
      }});

      updateAllCardTitles(document);
    }})();
    </script>
    """
    return body
