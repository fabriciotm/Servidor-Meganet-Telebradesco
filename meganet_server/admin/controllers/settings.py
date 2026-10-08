#!/usr/bin/env python3
"""Settings, reload and MegaNet content administrative controllers."""

from __future__ import annotations

import copy
import functools
import html
import logging
import pathlib
import threading
import urllib.parse

from ..helpers import (
    _admin_opt, _admin_yes, _request_close_meganet_sessions, _safe_field_value,
)
from ..meganet import _read_cadastro_users
from ..page import html_page
from ...config import (
    CONFIG_MANAGED_FIELDS, _coerce_config_value, _resolve_runtime_paths, apply_config_to_args,
    current_config_from_args, normalize_endpoint_args, read_config_file, refresh_app_config, write_config_file,
)
from ...meganet.content import (
    CONTENT_RECORD_DEFINITIONS, DEFAULT_CONTENT_DIR, MAX_CONTENT_GROUP_SAFE_BYTES,
    MAX_CONTENT_RECORD_LINES, MAX_CONTENT_RECORD_PAGES, MAX_CONTENT_RECORD_TOTAL_LINES, MAX_MESSAGE_LINE_LEN,
    content_budget_report, count_normalized_content_lines, merge_content_records,
    normalize_content_record_lines, normalize_message_lines, strip_content_mapping_lines,
)
from ...meganet.content_store import (
    CONTENT_FILE_FIELDS, _propagate_meganet_content_to_service_args, content_file_path,
    list_content_json_files, reload_active_meganet_content, select_content_json_file,
    write_content_file,
)
from ...meganet.mail import DEFAULT_WELCOME_EMAIL_BODY, DEFAULT_WELCOME_EMAIL_SUBJECT


_ADMIN_SETTINGS_WRITE_LOCK = threading.RLock()


def _settings_transaction(func):
    """Serialize administrative mutations of the shared runtime/config state."""

    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        with _ADMIN_SETTINGS_WRITE_LOCK:
            return func(self, *args, **kwargs)

    return wrapped

class SettingsControllerMixin:
    """Settings/content route controller mixin."""

    def _get_settings(self, parsed, current_user=None) -> None:
        cfg = current_config_from_args(self.app_context.args)
        body = f"""
        <form method='post' action='/settings'>
          <div class='grid'>
            <div class='card'>
              <h3>Admin</h3>
              <label>E-mail admin<input name='admin_email' value='{_safe_field_value(cfg.get('admin_email',''))}'></label>
              <label>Domínio Mega Net<input name='mail_domain' value='{_safe_field_value(cfg.get('mail_domain',''))}'></label>
              <p class='muted'>Usado como remetente do gerenciador e domínio dos e-mails internos.</p>
            </div>
            <div class='card'>
              <h3>Servidor Web</h3>
              <div class='inline'><label><input type='checkbox' name='admin_web' value='true' {_admin_yes(cfg.get('admin_web'))}> Habilitar interface web</label></div>
              <label>Host web<input name='admin_host' value='{_safe_field_value(cfg.get('admin_host',''))}'></label>
              <label>Porta web<input name='admin_port' value='{_safe_field_value(cfg.get('admin_port',''))}'></label>
              <p class='muted'>Alterações de host/porta da interface web exigem reinício.</p>
            </div>
          </div>

          <div class='card'>
            <div class='inline'><label><input id='show_advanced' type='checkbox' onchange="document.getElementById('advancedBox').classList.toggle('show', this.checked)"> Exibir Configurações Avançadas?</label></div>
            <p class='warn'>Alterações podem fazer o serviço não funcionar corretamente.</p>
            <div id='advancedBox' class='advanced-settings'>
              <div class='grid'>
                <div class='card' id='transportes'>
                  <h3 style='color:#55a8ff'>Transporte MegaNet</h3>
                  <label>Modo<select name='meganet_transport'>{_admin_opt('meganet_transport',['off','tcp','serial','both'], cfg)}</select></label>
                  <label>Host TCP<input name='meganet_host' value='{_safe_field_value(cfg.get('meganet_host','127.0.0.1'))}'></label>
                  <p class='small'>Use <code>0.0.0.0</code> para aceitar conexões em todas as interfaces IPv4.</p>
                  <label>Porta TCP<input name='meganet_port' value='{_safe_field_value(cfg.get('meganet_port','19777'))}'></label>
                  <label>Porta COM<input name='meganet_serial_port' value='{_safe_field_value(cfg.get('meganet_serial_port','COM12'))}'></label>
                  <label>Baud rate<input name='meganet_serial_baud' value='{_safe_field_value(cfg.get('meganet_serial_baud','115200'))}'></label>
                  <p class='muted'>O modo <b>both</b> aceita simultaneamente clientes TCP e um dispositivo serial MegaNet.</p>
                </div>
                <div class='card'>
                  <h3 style='color:#b985ff'>Transporte Telebradesco</h3>
                  <label>Modo<select name='telebradesco_transport'>{_admin_opt('telebradesco_transport',['off','tcp','serial','both'], cfg)}</select></label>
                  <label>Host TCP<input name='telebradesco_host' value='{_safe_field_value(cfg.get('telebradesco_host','127.0.0.1'))}'></label>
                  <p class='small'>Use <code>0.0.0.0</code> para aceitar conexões em todas as interfaces IPv4.</p>
                  <label>Porta TCP<input name='telebradesco_port' value='{_safe_field_value(cfg.get('telebradesco_port','19779'))}'></label>
                  <label>Porta COM<input name='telebradesco_serial_port' value='{_safe_field_value(cfg.get('telebradesco_serial_port','COM10'))}'></label>
                  <label>Baud rate<input name='telebradesco_serial_baud' value='{_safe_field_value(cfg.get('telebradesco_serial_baud','115200'))}'></label>
                  <p class='muted'>Cada serviço deve usar uma porta TCP e/ou COM diferente. Requer <code>pyserial</code> para COM.</p>
                </div>
                <div class='card'>
                  <h3>Autenticação e cadastro</h3>
                  <label>Modo MegaNet<select name='meganet_auth_mode'>{_admin_opt('meganet_auth_mode',['off','allow-new','require-known'], cfg)}</select></label>
                  <label>Comparação de senha<select name='meganet_password_case'>{_admin_opt('meganet_password_case',['exact','upper','lower','case-insensitive'], cfg)}</select></label>
                  <label>Usuário duplicado<select name='registration_duplicate_action'>{_admin_opt('registration_duplicate_action',['reject-message','allow-update'], cfg)}</select></label>
                  <label>Código de acesso duplicado<select name='codigo_acesso_duplicate_action'>{_admin_opt('codigo_acesso_duplicate_action',['reject-message','allow'], cfg)}</select></label>
                  <div class='inline'><label><input type='checkbox' name='password_change_apply' value='true' {_admin_yes(cfg.get('password_change_apply'))}> Aplicar troca de senha</label></div>
                </div>
                <div class='card'>
                  <h3>Protocolo</h3>
                  <label>Ação pós-ID<select name='post_id_action'>{_admin_opt('post_id_action',['none','ready','message-ready','id-body-hash-end-ready2-wait'], cfg)}</select></label>
                  <p class='muted'><b>Revista Eletrônica completa:</b> o servidor envia <code>#abril#</code> seguido de <code>#ready#</code> após o ID, ativando o menu nativo <code>$016FF2</code> e os 11 slots fixos de Comics/RPG.</p>
                  <label>Delay pós-ID<input name='post_id_message_delay' value='{_safe_field_value(cfg.get('post_id_message_delay',''))}'></label>
                  <label>Máx. linha<input type='number' min='8' max='{MAX_MESSAGE_LINE_LEN}' name='outbound_line_max_len' value='{_safe_field_value(cfg.get('outbound_line_max_len',MAX_MESSAGE_LINE_LEN))}'></label><p class='muted'>O limite visual seguro é {MAX_MESSAGE_LINE_LEN} caracteres. Com 36 caracteres exatos, a ROM quebra automaticamente e o CR avança outra linha.</p>
                  <label>Delay pacote<input name='outbound_packet_delay' value='{_safe_field_value(cfg.get('outbound_packet_delay',''))}'></label>
                  <label>Confirmação ACK/NAK<select name='meganet_packet_ack_mode'>{_admin_opt('meganet_packet_ack_mode',['off','transfers','all'], cfg)}</select></label>
                  <label>Timeout do ACK (s)<input type='number' min='0.2' max='30' step='0.1' name='meganet_packet_ack_timeout' value='{_safe_field_value(cfg.get('meganet_packet_ack_timeout',3.0))}'></label>
                  <label>Tentativas por pacote<input type='number' min='1' max='10' name='meganet_packet_max_retries' value='{_safe_field_value(cfg.get('meganet_packet_max_retries',5))}'></label>
                  <p class='muted'><b>all</b> (recomendado): exige ACK para todo pacote servidor→cartucho. <b>transfers</b>: compatibilidade, controla somente notícias e correio. NAK ou timeout retransmite o mesmo frame e a mesma sequência; falha persistente encerra a conexão.</p>
                  <label>Ruídos tolerados no bootstrap<input type='number' min='0' max='32' name='meganet_bootstrap_noise_tolerance' value='{_safe_field_value(cfg.get('meganet_bootstrap_noise_tolerance',2))}'></label>
                  <label>Janela dos ruídos (s)<input type='number' min='0.1' max='30' step='0.1' name='meganet_bootstrap_noise_window' value='{_safe_field_value(cfg.get('meganet_bootstrap_noise_window',2.0))}'></label>
                  <p class='muted'>Bytes inválidos isolados são descartados. Ao exceder a tolerância dentro da janela, o TCP é fechado. Use <b>2 bytes em 2 segundos</b> para troncos SIP; use <b>0</b> para restaurar o encerramento no primeiro byte inválido.</p>
                  <label>Token desconhecido<select name='unknown_token_action'>{_admin_opt('unknown_token_action',['none','ready','message-ready'], cfg)}</select></label>
                  <label>ATH0 em dados<select name='raw_hayes_in_data_action'>{_admin_opt('raw_hayes_in_data_action',['ignore','close','ok-close'], cfg)}</select></label>
                </div>
                <div class='card'>
                  <h3>Correio</h3>
                  <label>Contagem no painel<select name='connection_panel_mail_count'>{_admin_opt('connection_panel_mail_count',['auto','off'], cfg)}</select></label>
                </div>
                <div class='card'>
                  <h3>E-mail de boas-vindas</h3>
                  <div class='inline'><label><input type='checkbox' name='welcome_email_enabled' value='true' {_admin_yes(cfg.get('welcome_email_enabled', True))}> Enviar e-mail ao concluir cadastro</label></div>
                  <label>Remetente<input name='welcome_email_from' value='{_safe_field_value(cfg.get('welcome_email_from') or cfg.get('admin_email',''))}'></label>
                  <label>Assunto<input name='welcome_email_subject' value='{_safe_field_value(cfg.get('welcome_email_subject', DEFAULT_WELCOME_EMAIL_SUBJECT))}'></label>
                  <label>Mensagem<textarea name='welcome_email_body' rows='7'>{html.escape(str(cfg.get('welcome_email_body') or "\n".join(DEFAULT_WELCOME_EMAIL_BODY)))}</textarea></label>
                  <p class='muted'>Placeholders disponíveis: <code>{{usuario}}</code>, <code>{{nome_meganet}}</code>, <code>{{email_meganet}}</code>, <code>{{nome}}</code>, <code>{{apelido}}</code>, <code>{{codigo_acesso}}</code>, <code>{{senha}}</code>, <code>{{cidade}}</code>, <code>{{estado}}</code>, <code>{{data}}</code> e <code>{{hora}}</code>.</p>
                  <p class='muted'>Use mensagens curtas, com uma informação por linha, para melhor leitura no Correio MegaNet.</p>
                </div>
                <div class='card'>
                  <h3>Histórico MegaNet</h3>
                  <div class='inline'><label><input type='checkbox' name='enable_session_logs' value='true' {_admin_yes(cfg.get('enable_session_logs'))}> Habilitar logs de sessão do MegaNet</label></div>
                  <p class='muted'>Quando habilitado, o servidor preserva o histórico das sessões MegaNet para consulta administrativa.</p>
                </div>
                <div class='card'>
                  <h3>Telebradesco</h3>
                  <label>Bloqueio de conta<select name='telebradesco_block_mode'>{_admin_opt('telebradesco_block_mode',['native-020','deny-login'], cfg)}</select></label>
                  <label>Retenção de erro terminal (segundos)<input name='telebradesco_error_hold_seconds' type='number' min='0' max='5' step='0.1' value='{html.escape(str(cfg.get("telebradesco_error_hold_seconds", 2.0)))}'></label>
                  <p class='muted'><b>native-020</b>: mantém a ROM original e não envia conteúdo da conta bloqueada. Os estados da Conta Corrente e da Poupança recebem 020; as seções 2 e 9 ficam ausentes. Ao abrir <b>Conta Corrente → Saldo</b> ou <b>Poupança → Saldo</b>, a ROM exibe <b>C/C BLOQUEADA</b>.</p>
                  <p class='muted'><b>deny-login</b>: recusa o login imediatamente com o código terminal 010. A mensagem fixa da ROM é <b>SENHA NAO GRAVADA</b>.</p>
                  <p class='muted'>A retenção atrasa a resposta ao ATH0 somente para erros terminais. Ela não altera a duração de C/C BLOQUEADA, que é exibida localmente após a conexão.</p>
                </div>
                <div class='card'>
                  <h3>Registro do servidor</h3>
                  <label>Nível<select name='log_level'>{_admin_opt('log_level',['DEBUG','INFO','WARNING','ERROR'], cfg)}</select></label>
                </div>
              </div>
            </div>
          </div>
          <button type='submit'>Salvar configurações</button>
        </form>"""
        return self._send(200, html_page("Configurações", body))

    def _get_content(self, parsed, current_user=None) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        selected_file = query.get("content_file", [""])[0].strip()
        if selected_file:
            selected_path = select_content_json_file(self.app_context.args, selected_file)
            if selected_path is not None:
                try:
                    write_config_file(self.app_context.config_path, self.app_context.args)
                except (OSError, TypeError, ValueError) as exc:
                    logging.warning("CONTENT_SELECTION_CONFIG_SAVE_FAILED file=%s error=%s", selected_file, exc)
            else:
                logging.warning("CONTENT_SELECTION_IGNORED invalid_file=%r", selected_file)
        reload_active_meganet_content(self.app_context.args, create_if_missing=True)
        content_path = content_file_path(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR))
        content_files = list_content_json_files(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR))
        active_content_file = content_path.name
        content_file_admin_options = "".join(
            f"<option value='{html.escape(name)}' {'selected' if name == active_content_file else ''}>{html.escape(name)}</option>"
            for name in content_files
        )
        content_selector_html = f"""
        <div class='card content-file-card'>
          <form method='get' action='/content' class='content-file-selector'>
            <label>Arquivo de conteúdo
              <select name='content_file' onchange='this.form.submit()'>
                {content_file_admin_options}
              </select>
            </label>
            <button type='submit' class='secondary'>Carregar arquivo</button>
          </form>
          <p class='muted'>Selecione qual arquivo JSON de conteúdo será usado pelo MegaNet. Todos os arquivos <code>.json</code> encontrados em <code>meganet\\content</code> aparecem nesta lista.</p>
        </div>
        """
        panel = "\n".join(str(x) for x in getattr(self.app_context.args, "connection_panel_lines", []) or [])
        message = "\n".join(str(x) for x in getattr(self.app_context.args, "message_lines", []) or [])
        content_records = merge_content_records(getattr(self.app_context.args, "content_records", None))
        current_budget_report = content_budget_report(content_records)
        panel_maxlines_attr = str(MAX_CONTENT_RECORD_LINES)
        content_record_maxlines_attr = str(MAX_CONTENT_RECORD_TOTAL_LINES)
        content_record_maxlength = (
            MAX_MESSAGE_LINE_LEN * MAX_CONTENT_RECORD_TOTAL_LINES
            + max(0, MAX_CONTENT_RECORD_TOTAL_LINES - 1)
        )
        budget_cards_html = "".join(
            f"""
            <div class='card content-budget-card' data-budget-profile='{html.escape(profile)}' data-budget-limit='{MAX_CONTENT_GROUP_SAFE_BYTES}'>
              <h4>{html.escape(str(report.get('label', profile)))}</h4>
              <progress class='content-budget-progress' max='{MAX_CONTENT_GROUP_SAFE_BYTES}' value='{min(MAX_CONTENT_GROUP_SAFE_BYTES, int(report.get('total_bytes', 0)))}' style='width:100%'></progress>
              <p class='help'><span class='group-budget-counter'>{int(report.get('total_bytes', 0)):,} / {MAX_CONTENT_GROUP_SAFE_BYTES:,} bytes</span></p>
            </div>"""
            for profile, report in current_budget_report.items()
        )

        notices: list[str] = []
        if query.get("saved"):
            notices.append("<p class='ok'><b>Conteúdo salvo com sucesso.</b></p>")
        if query.get("reloaded"):
            notices.append("<p class='ok'><b>Servidor recarregado.</b> Configuração e conteúdo foram reaplicados.</p><p class='muted'>Se forem alteradas configurações avançadas, como host, porta, transporte, porta COM ou parâmetros estruturais do servidor, o script deverá ser reiniciado para garantir que tudo seja aplicado corretamente.</p>")
        truncated_count = query.get("truncated", [""])[0]
        if truncated_count:
            notices.append(f"<p class='warn'>Alguns campos ultrapassaram o limite configurado e foram truncados. Quantidade de campos ajustados: {html.escape(truncated_count)}.</p>")
        notices_html = "<div class='card'>" + "".join(notices) + "</div>" if notices else ""

        def _textarea_for(item: dict) -> str:
            key = str(item["key"])
            value_lines = normalize_content_record_lines(content_records.get(key, item.get("default", [])), max_lines=MAX_CONTENT_RECORD_TOTAL_LINES)
            value = "\n".join(str(x) for x in value_lines)
            title = html.escape(str(item.get("menu", key)))
            profile = str(item.get("profile", ""))
            return f"""
            <div class='content-item'>
              <h4>{title}</h4>
              <textarea data-maxline='{MAX_MESSAGE_LINE_LEN}' data-maxlines='{html.escape(content_record_maxlines_attr)}' data-pagesize='{MAX_CONTENT_RECORD_LINES}' data-maxpages='{MAX_CONTENT_RECORD_PAGES}' data-profile='{html.escape(profile)}' data-record-key='{html.escape(key)}' maxlength='{content_record_maxlength}' rows='24' wrap='off' name='content_record.{html.escape(key)}'>{html.escape(value)}</textarea><p class='help'><span class='line-counter'></span><br>Linha em branco: deixe uma linha vazia ou use &lt;BR&gt;.</p>
            </div>"""

        def _content_tree_labels(item: dict) -> tuple[str, str, str]:
            root = str(item.get("grupo", "GERAL") or "GERAL")
            subgroup = str(item.get("subgrupo", root) or root)
            return root, subgroup, ""

        def _content_grid_class(root: str, subgroup: str, items: list[dict]) -> str:
            root_norm = str(root or "").strip().upper()
            subgroup_norm = str(subgroup or "").strip().upper()
            if root_norm == "REVISTA" and subgroup_norm in {"COMICS", "RPG"}:
                return "grid content-grid-three"
            if len(items) == 8 and ((root_norm == "TECTOY NEWS" and subgroup_norm == "PRODUTOS") or root_norm == "SEGA CLUB" or subgroup_norm == "SEGA CLUB"):
                return "grid content-grid-four"
            return "grid"

        tree: dict[str, dict[str, dict[str, list[dict]]]] = {}
        for item in CONTENT_RECORD_DEFINITIONS:
            root, subgroup, category = _content_tree_labels(item)
            tree.setdefault(root, {}).setdefault(subgroup, {}).setdefault(category, []).append(item)

        menu_sections: list[str] = []
        for root, subgroups in tree.items():
            sub_html: list[str] = []
            for subgroup, categories in subgroups.items():
                if len(categories) == 1 and "" in categories:
                    sub_body = f"<div class='{_content_grid_class(root, subgroup, categories[''])}'>{''.join(_textarea_for(item) for item in categories[''])}</div>"
                else:
                    cat_html: list[str] = []
                    for category, items in categories.items():
                        if category:
                            cat_html.append(f"""
                            <details class='category' open>
                              <summary>{html.escape(category)}</summary>
                              <div class='{_content_grid_class(root, subgroup, items)}'>{''.join(_textarea_for(item) for item in items)}</div>
                            </details>""")
                        else:
                            cat_html.append(f"<div class='{_content_grid_class(root, subgroup, items)}'>{''.join(_textarea_for(item) for item in items)}</div>")
                    sub_body = "".join(cat_html)
                sub_html.append(f"""
                <details class='subgroup' open>
                  <summary>{html.escape(subgroup)}</summary>
                  {sub_body}
                </details>""")
            menu_sections.append(f"""
            <details class='menu-group' open>
              <summary>{html.escape(root)}</summary>
              {''.join(sub_html)}
            </details>""")

        content_actions_html = f"""
        <div class='card toolbar content-actions'>
          <button type='submit' form='contentForm' class='okbtn'>Salvar conteúdo</button>
          <form method='post' action='/reload' style='margin:0'>
            <input type='hidden' name='return_to' value='/content'>
            <input type='hidden' name='selected_content_file' value='{html.escape(active_content_file)}'>
            <button class='bluebtn' type='submit'>Recarregar servidor</button>
          </form>
          <span class='muted'>Recarrega as configurações e os conteúdos salvos.</span>
        </div>
        """

        body = f"""
        {notices_html}
        {content_selector_html}
        {content_actions_html}
        <form id='contentForm' method='post' action='/content/save'>
          <input type='hidden' name='selected_content_file' value='{html.escape(active_content_file)}'>
          <div class='grid'>
            <div class='card'><h3>Painel de conexão</h3><textarea data-maxline='{MAX_MESSAGE_LINE_LEN}' data-maxlines='{html.escape(panel_maxlines_attr)}' maxlength='{MAX_MESSAGE_LINE_LEN * MAX_CONTENT_RECORD_LINES + max(0, MAX_CONTENT_RECORD_LINES - 1)}' rows='19' wrap='off' name='connection_panel_lines'>{html.escape(panel)}</textarea><p class='muted'>Limite seguro: {MAX_MESSAGE_LINE_LEN} caracteres por linha e 19 linhas. Placeholders: {{usuario}}, {{email}}, {{mail_count}}, {{mail_status}}</p><p class='help'><span class='line-counter'></span></p></div>
            <div class='card'><h3>Mensagem de boas-vindas/cadastro</h3><textarea data-maxline='{MAX_MESSAGE_LINE_LEN}' data-maxlines='{html.escape(panel_maxlines_attr)}' maxlength='{MAX_MESSAGE_LINE_LEN * MAX_CONTENT_RECORD_LINES + max(0, MAX_CONTENT_RECORD_LINES - 1)}' rows='19' wrap='off' name='message_lines'>{html.escape(message)}</textarea><p class='muted'>Limite seguro: {MAX_MESSAGE_LINE_LEN} caracteres por linha e 19 linhas.</p><p class='help'><span class='line-counter'></span></p></div>
          </div>
          <div class='card'>
            <h3>Conteúdo dos grupos</h3>
            <p class='muted'>As seções estão organizadas em árvore e são abertas automaticamente para facilitar a revisão.</p>
            <p class='muted'>Cada linha pode ter até <b>{MAX_MESSAGE_LINE_LEN} caracteres</b>. O tamanho recomendado continua sendo <b>{MAX_CONTENT_RECORD_LINES} linhas</b>; o máximo validado é <b>{MAX_CONTENT_RECORD_TOTAL_LINES} linhas</b> por item, correspondentes a duas telas completas e 12 linhas adicionais.</p>
            <p class='muted'>Cada grupo protocolar possui orçamento seguro de <b>{MAX_CONTENT_GROUP_SAFE_BYTES:,} bytes</b>, com margem abaixo do buffer de 16 KiB observado na ROM. O salvamento é bloqueado se o conjunto exceder esse valor.</p>
            <div class='grid'>{budget_cards_html}</div>
            {''.join(menu_sections)}
          </div>
        </form>
        {content_actions_html}
        <script>
        (function(){{
          const DEFAULT_MAX_CHARS = 36;
          const DEFAULT_MAX_LINES = 19;

          function normalizeNewlines(value){{
            return String(value || '').replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n');
          }}

          function getLimits(el){{
            const maxChars = Math.max(1, parseInt(el.dataset.maxline || DEFAULT_MAX_CHARS, 10) || DEFAULT_MAX_CHARS);
            const maxLines = Math.max(1, parseInt(el.dataset.maxlines || DEFAULT_MAX_LINES, 10) || DEFAULT_MAX_LINES);
            return {{maxChars: maxChars, maxLines: maxLines}};
          }}

          function buildLimitedLines(el){{
            const limits = getLimits(el);
            const raw = normalizeNewlines(el.value).split('\\n');
            const out = [];
            let trimmed = false;
            for (let r = 0; r < raw.length; r++) {{
              const line = raw[r];
              if (line.length === 0) {{
                out.push('');
              }} else {{
                for (let i = 0; i < line.length; i += limits.maxChars) {{
                  out.push(line.slice(i, i + limits.maxChars));
                  if (out.length >= limits.maxLines && (i + limits.maxChars < line.length || r < raw.length - 1)) {{
                    trimmed = true;
                    break;
                  }}
                }}
              }}
              if (out.length >= limits.maxLines) {{
                if (r < raw.length - 1) trimmed = true;
                break;
              }}
            }}
            if (!out.length) out.push('');
            if (out.length > limits.maxLines) {{
              out.length = limits.maxLines;
              trimmed = true;
            }}
            return {{lines: out, trimmed: trimmed, maxChars: limits.maxChars, maxLines: limits.maxLines}};
          }}

          function effectiveLines(state){{
            return state.lines.map(function(line){{
              return String(line || '').replace(/#/g, '').trim();
            }}).filter(function(line){{ return line.length > 0; }});
          }}

          function updateCounter(el, state){{
            const target = el.parentElement.querySelector('.line-counter');
            if (!target) return;
            const pageSize = Math.max(0, parseInt(el.dataset.pagesize || '0', 10) || 0);
            const usedLines = effectiveLines(state).length;
            let pageInfo = '';
            if (pageSize > 0) {{
              const usedBlocks = Math.max(1, Math.ceil(usedLines / pageSize));
              pageInfo = ', ' + usedBlocks + ' bloco(s) visual(is) de até ' + pageSize + ' linhas';
            }}
            const extended = usedLines > pageSize && pageSize > 0 ? ' — conteúdo estendido' : '';
            target.textContent = usedLines + '/' + state.maxLines + ' linhas' + pageInfo + ', máx. ' + state.maxChars + ' caracteres por linha' + extended + (state.trimmed ? ' — excedente removido' : '');
          }}

          function calculateGroupBytes(profile){{
            const fields = Array.from(document.querySelectorAll("textarea[data-profile='" + profile + "']"));
            let bodyBytes = 0;
            fields.forEach(function(el){{
              const state = buildLimitedLines(el);
              effectiveLines(state).forEach(function(line){{
                bodyBytes += line.length + 1;
              }});
            }});
            const statusBytes = fields.length ? Math.max(0, fields.length - 1) * 8 + 7 : 0;
            return {{bodyBytes: bodyBytes, statusBytes: statusBytes, totalBytes: bodyBytes + statusBytes, records: fields.length}};
          }}

          function updateAllGroupBudgets(){{
            let valid = true;
            document.querySelectorAll('[data-budget-profile]').forEach(function(card){{
              const profile = card.dataset.budgetProfile || '';
              const limit = Math.max(1, parseInt(card.dataset.budgetLimit || '1', 10) || 1);
              const used = calculateGroupBytes(profile);
              const progress = card.querySelector('.content-budget-progress');
              const counter = card.querySelector('.group-budget-counter');
              const over = used.totalBytes > limit;
              valid = valid && !over;
              if (progress) progress.value = Math.min(limit, used.totalBytes);
              if (counter) {{
                counter.textContent = used.totalBytes.toLocaleString('pt-BR') + ' / ' + limit.toLocaleString('pt-BR') + ' bytes — ' + Math.max(0, limit - used.totalBytes).toLocaleString('pt-BR') + ' disponíveis';
                counter.className = over ? 'group-budget-counter warn' : 'group-budget-counter help';
                if (over) counter.textContent += ' — LIMITE EXCEDIDO';
              }}
            }});
            document.querySelectorAll("button[form='contentForm']").forEach(function(button){{
              button.disabled = !valid;
              button.title = valid ? '' : 'Reduza o conteúdo de um dos grupos antes de salvar.';
            }});
            return valid;
          }}

          function enforceTextAreaLimit(el){{
            const original = normalizeNewlines(el.value);
            const state = buildLimitedLines(el);
            const next = state.lines.join('\\n');
            if (next !== original) {{
              const pos = el.selectionStart || next.length;
              el.value = next;
              const newPos = Math.min(next.length, Math.max(0, pos + (next.length - original.length)));
              try {{ el.setSelectionRange(newPos, newPos); }} catch (e) {{}}
            }}
            updateCounter(el, state);
            updateAllGroupBudgets();
          }}

          function insertTextAtCursor(el, text){{
            const value = normalizeNewlines(el.value);
            const start = el.selectionStart || 0;
            const end = el.selectionEnd || start;
            el.value = value.slice(0, start) + text + value.slice(end);
            const pos = start + text.length;
            try {{ el.setSelectionRange(pos, pos); }} catch (e) {{}}
          }}

          function handleAutoLineBreak(el, ev){{
            if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
            const limits = getLimits(el);
            const value = normalizeNewlines(el.value);
            const start = el.selectionStart || 0;
            const end = el.selectionEnd || start;
            const before = value.slice(0, start);
            const lineStart = before.lastIndexOf('\\n') + 1;
            const lineNumber = before.split('\\n').length;
            const currentLineBeforeCursor = value.slice(lineStart, start);
            const lineCount = value.length ? value.split('\\n').length : 1;

            if (ev.key === 'Enter') {{
              if (start === end && lineCount >= limits.maxLines) {{
                ev.preventDefault();
              }}
              return;
            }}

            if (ev.key.length !== 1) return;

            if (start === end && currentLineBeforeCursor.length >= limits.maxChars) {{
              ev.preventDefault();
              if (lineNumber >= limits.maxLines) {{
                enforceTextAreaLimit(el);
                return;
              }}
              insertTextAtCursor(el, '\\n' + ev.key);
              enforceTextAreaLimit(el);
            }}
          }}

          document.querySelectorAll('textarea[data-maxline]').forEach(function(el){{
            if (!el.dataset.maxlines) el.dataset.maxlines = String(DEFAULT_MAX_LINES);
            const limits = getLimits(el);
            el.setAttribute('maxlength', String(limits.maxChars * limits.maxLines + Math.max(0, limits.maxLines - 1)));
            el.setAttribute('wrap', 'off');
            el.addEventListener('keydown', function(ev){{ handleAutoLineBreak(el, ev); }});
            el.addEventListener('input', function(){{ enforceTextAreaLimit(el); }});
            el.addEventListener('paste', function(){{ setTimeout(function(){{ enforceTextAreaLimit(el); }}, 0); }});
            enforceTextAreaLimit(el);
          }});
          const contentForm = document.getElementById('contentForm');
          if (contentForm) {{
            contentForm.addEventListener('submit', function(ev){{
              if (!updateAllGroupBudgets()) {{
                ev.preventDefault();
                alert('O conteúdo excede o orçamento seguro de um dos grupos. Reduza o texto indicado antes de salvar.');
              }}
            }});
          }}
          updateAllGroupBudgets();
        }})();
        </script>"""
        return self._send(200, html_page("Conteúdo Mega Net", body))

    @_settings_transaction
    def _post_service_select(self, parsed, form, current_user=None) -> None:
        selected = str(form.get("active_service", [""])[0]).strip().lower()
        if selected not in {"meganet", "telebradesco"}:
            return self._send(400, html_page("Serviço inválido", "<p class='warn'>Selecione Mega Net ou Telebradesco.</p>"))
        previous = str(getattr(self.app_context.args, "active_service", "meganet") or "meganet")
        self.app_context.args.active_service = selected
        try:
            write_config_file(self.app_context.config_path, self.app_context.args)
        except (OSError, TypeError, ValueError) as exc:
            self.app_context.args.active_service = previous
            logging.error("ACTIVE_SERVICE_SAVE_FAILED previous=%s requested=%s error=%s", previous, selected, exc)
            return self._send(500, html_page("Falha ao salvar serviço", "<p class='warn'>Não foi possível persistir a troca de serviço. O serviço ativo foi mantido sem alteração.</p><p><a href='/'>Voltar</a></p>"))
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        closed = runtime.request_close_all(f"troca de serviço {previous} -> {selected}") if runtime else 0
        logging.info("ACTIVE_SERVICE_CHANGED previous=%s current=%s sessions_requested_close=%s", previous, selected, closed)
        return self._redirect("/?service_changed=1")

    @_settings_transaction
    def _post_settings(self, parsed, form, current_user=None) -> None:
        previous_service = str(getattr(self.app_context.args, "active_service", "meganet") or "meganet")
        previous_values = {
            key: copy.deepcopy(getattr(self.app_context.args, key))
            for key in CONFIG_MANAGED_FIELDS
            if hasattr(self.app_context.args, key)
        }
        had_app_config = hasattr(self.app_context.args, "app_config")
        previous_app_config = getattr(self.app_context.args, "app_config", None)
        had_config_base_dir = hasattr(self.app_context.args, "_config_base_dir")
        previous_config_base_dir = getattr(self.app_context.args, "_config_base_dir", None)
        update: dict[str, object] = {}
        content_fields = CONTENT_FILE_FIELDS
        bool_fields = {"online_start", "enable_session_logs", "password_change_apply", "admin_web", "welcome_email_enabled"}
        for key in CONFIG_MANAGED_FIELDS:
            if key in content_fields or key in bool_fields:
                continue
            if key in form:
                update[key] = form[key][0]
        for b in bool_fields:
            update[b] = b in form
        for key, value in update.items():
            try:
                setattr(self.app_context.args, key, _coerce_config_value(key, value))
            except (OSError, TypeError, ValueError) as exc:
                logging.warning("ADMIN_SETTING_IGNORED key=%s value=%r error=%s", key, value, exc)
        if "active_service" in update:
            selected_service = str(getattr(self.app_context.args, "active_service", "meganet") or "meganet").lower()
            if selected_service not in {"meganet", "telebradesco"}:
                self.app_context.args.active_service = "meganet"
        normalize_endpoint_args(self.app_context.args)
        _resolve_runtime_paths(self.app_context.args, self.app_context.config_path.parent)
        refresh_app_config(self.app_context.args)
        try:
            write_config_file(self.app_context.config_path, self.app_context.args)
        except (OSError, TypeError, ValueError) as exc:
            for key, value in previous_values.items():
                setattr(self.app_context.args, key, value)
            if had_app_config:
                self.app_context.args.app_config = previous_app_config
            elif hasattr(self.app_context.args, "app_config"):
                delattr(self.app_context.args, "app_config")
            if had_config_base_dir:
                self.app_context.args._config_base_dir = previous_config_base_dir
            elif hasattr(self.app_context.args, "_config_base_dir"):
                delattr(self.app_context.args, "_config_base_dir")
            logging.error("ADMIN_SETTINGS_SAVE_FAILED config=%s error=%s", self.app_context.config_path, exc)
            return self._send(500, html_page("Falha ao salvar configurações", "<p class='warn'>Não foi possível persistir as configurações. Os valores ativos foram mantidos sem alteração.</p><p><a href='/settings'>Voltar</a></p>"))
        _propagate_meganet_content_to_service_args(self.app_context.args)
        requested_level = str(getattr(self.app_context.args, "log_level", "INFO") or "INFO").upper()
        logging.getLogger().setLevel(getattr(logging, requested_level, logging.INFO))
        logging.info("ADMIN_LOG_LEVEL_APPLIED level=%s", requested_level)
        if "active_service" in update and str(getattr(self.app_context.args, "active_service", "meganet")) != previous_service:
            runtime = getattr(self.app_context.args, "_service_runtime", None)
            if runtime:
                runtime.request_close_all("serviço alterado pelas configurações")
        if "content_dir" in update:
            try:
                write_content_file(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR), self.app_context.args)
            except (OSError, TypeError, ValueError) as exc:
                logging.warning("ADMIN_CONTENT_DIR_INIT_FAILED content_dir=%s error=%s", getattr(self.app_context.args, "content_dir", ""), exc)
        return self._send(200, html_page("Configurações salvas", "<p class='ok'>Configurações atualizadas. Host/portas e transporte exigem reinício.</p><p><a href='/settings'>Voltar</a></p>"))

    @_settings_transaction
    def _post_content_save(self, parsed, form, current_user=None) -> None:
        selected_file = form.get("selected_content_file", [""])[0].strip()
        if selected_file:
            previous_content_dir = getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR)
            selected_path = select_content_json_file(self.app_context.args, selected_file)
            if selected_path is None:
                return self._send(400, html_page("Arquivo de conteúdo inválido", "<p class='warn'>Seleção de conteúdo inválida.</p><p><a href='/content'>Voltar</a></p>"))
            try:
                write_config_file(self.app_context.config_path, self.app_context.args)
            except (OSError, TypeError, ValueError) as exc:
                self.app_context.args.content_dir = previous_content_dir
                logging.error("CONTENT_SELECTION_CONFIG_SAVE_FAILED file=%s error=%s action=restore-selection", selected_file, exc)
                return self._send(500, html_page("Falha ao salvar seleção de conteúdo", "<p class='warn'>Não foi possível persistir a seleção do arquivo de conteúdo. O arquivo ativo foi mantido sem alteração.</p><p><a href='/content'>Voltar</a></p>"))
        truncated_keys: list[str] = []
        candidate_panel_lines = list(getattr(self.app_context.args, "connection_panel_lines", []) or [])
        candidate_message_lines = list(getattr(self.app_context.args, "message_lines", []) or [])
        if "connection_panel_lines" in form:
            raw_panel_lines = form["connection_panel_lines"][0].splitlines()
            full_panel_lines = normalize_message_lines(raw_panel_lines)
            if len(full_panel_lines) > MAX_CONTENT_RECORD_LINES:
                truncated_keys.append("connection_panel_lines")
            candidate_panel_lines = full_panel_lines[:MAX_CONTENT_RECORD_LINES]
        if "message_lines" in form:
            raw_message_lines = form["message_lines"][0].splitlines()
            full_message_lines = normalize_message_lines(raw_message_lines)
            if len(full_message_lines) > MAX_CONTENT_RECORD_LINES:
                truncated_keys.append("message_lines")
            candidate_message_lines = full_message_lines[:MAX_CONTENT_RECORD_LINES]

        candidate_records = merge_content_records(getattr(self.app_context.args, "content_records", None))
        for key in list(candidate_records.keys()):
            form_key = f"content_record.{key}"
            if form_key in form:
                raw_lines = strip_content_mapping_lines(form[form_key][0].splitlines())
                if count_normalized_content_lines(raw_lines) > MAX_CONTENT_RECORD_TOTAL_LINES:
                    truncated_keys.append(key)
                candidate_records[key] = normalize_content_record_lines(raw_lines, max_lines=MAX_CONTENT_RECORD_TOTAL_LINES)
        candidate_records = merge_content_records(candidate_records)

        budget_report = content_budget_report(candidate_records)
        over_budget = [report for report in budget_report.values() if report.get("over_limit")]
        if over_budget:
            details = "".join(
                f"<li><b>{html.escape(str(report.get('label', report.get('profile', 'grupo'))))}</b>: "
                f"{int(report.get('total_bytes', 0)):,} bytes usados; limite seguro de {MAX_CONTENT_GROUP_SAFE_BYTES:,} bytes; "
                f"excesso de {max(0, int(report.get('total_bytes', 0)) - MAX_CONTENT_GROUP_SAFE_BYTES):,} bytes.</li>"
                for report in over_budget
            )
            logging.warning(
                "ADMIN_CONTENT_SAVE_REJECTED reason=group-budget profiles=%s details=%s",
                ",".join(str(report.get("profile", "")) for report in over_budget),
                ";".join(f"{report.get('profile')}={report.get('total_bytes')}" for report in over_budget),
            )
            error_body = (
                "<p class='warn'><b>O conteúdo não foi salvo.</b> Um ou mais grupos ultrapassaram o orçamento seguro da ROM.</p>"
                f"<ul>{details}</ul>"
                "<p>Reduza o número de linhas de um dos itens indicados. O limite individual continua sendo 50 linhas, mas todos os itens do grupo compartilham o mesmo buffer.</p>"
                "<p><a href='/content'>Voltar ao editor</a></p>"
            )
            return self._send(400, html_page("Conteúdo acima do limite seguro", error_body))

        previous_panel_lines = list(getattr(self.app_context.args, "connection_panel_lines", []) or [])
        previous_message_lines = list(getattr(self.app_context.args, "message_lines", []) or [])
        previous_content_records = getattr(self.app_context.args, "content_records", None)
        self.app_context.args.connection_panel_lines = candidate_panel_lines
        self.app_context.args.message_lines = candidate_message_lines
        self.app_context.args.content_records = candidate_records
        try:
            write_content_file(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR), self.app_context.args)
        except (OSError, TypeError, ValueError) as exc:
            self.app_context.args.connection_panel_lines = previous_panel_lines
            self.app_context.args.message_lines = previous_message_lines
            self.app_context.args.content_records = previous_content_records
            logging.error("ADMIN_CONTENT_SAVE_FAILED content_dir=%s error=%s", getattr(self.app_context.args, "content_dir", ""), exc)
            return self._send(500, html_page("Falha ao salvar conteúdo", "<p class='warn'>Não foi possível persistir o conteúdo MegaNet. O conteúdo ativo foi mantido sem alteração.</p><p><a href='/content'>Voltar</a></p>"))
        _propagate_meganet_content_to_service_args(self.app_context.args)
        closed = _request_close_meganet_sessions(self.app_context.args, "conteúdo MegaNet salvo/recarregado")
        if closed:
            logging.info("ADMIN_CONTENT_SAVE_CLOSED_MEGANET sessions=%d", closed)
        suffix = "?saved=1"
        if truncated_keys:
            suffix += "&truncated=" + urllib.parse.quote(str(len(truncated_keys)))
        active_name = content_file_path(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR)).name
        if active_name:
            suffix += "&content_file=" + urllib.parse.quote(active_name)
        return self._redirect("/content" + suffix)

    @_settings_transaction
    def _post_reload(self, parsed, form, current_user=None) -> None:
        previous_service = str(getattr(self.app_context.args, "active_service", "meganet") or "meganet")
        cfg = read_config_file(self.app_context.config_path)
        apply_config_to_args(self.app_context.args, cfg, set())
        _resolve_runtime_paths(self.app_context.args, self.app_context.config_path.parent)
        refresh_app_config(self.app_context.args)
        self.app_context.args.active_service = str(getattr(self.app_context.args, "active_service", "meganet") or "meganet").lower()
        if self.app_context.args.active_service not in {"meganet", "telebradesco"}:
            self.app_context.args.active_service = "meganet"
        selected_file = form.get("selected_content_file", form.get("content_file", [""]))[0].strip()
        if selected_file:
            previous_content_dir = getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR)
            selected_path = select_content_json_file(self.app_context.args, selected_file)
            if selected_path is not None:
                try:
                    write_config_file(self.app_context.config_path, self.app_context.args)
                except (OSError, TypeError, ValueError) as exc:
                    self.app_context.args.content_dir = previous_content_dir
                    logging.error("CONTENT_SELECTION_CONFIG_SAVE_FAILED file=%s error=%s action=restore-selection", selected_file, exc)
                    return self._send(500, html_page("Falha ao salvar seleção de conteúdo", "<p class='warn'>Não foi possível persistir a seleção do arquivo de conteúdo. A seleção anterior foi mantida.</p><p><a href='/content'>Voltar</a></p>"))
            else:
                logging.warning("CONTENT_SELECTION_IGNORED invalid_file=%r", selected_file)
        if self.app_context.args.active_service != previous_service:
            runtime = getattr(self.app_context.args, "_service_runtime", None)
            if runtime:
                runtime.request_close_all("serviço alterado pelo recarregamento")
        active_content_path = reload_active_meganet_content(self.app_context.args, legacy_config=cfg, create_if_missing=True)
        closed = _request_close_meganet_sessions(self.app_context.args, "conteúdo/configuração MegaNet recarregado")
        if closed:
            logging.info("ADMIN_RELOAD_CLOSED_MEGANET sessions=%d", closed)
        users = _read_cadastro_users(pathlib.Path(self.app_context.args.cadastro_db))
        logging.info("ADMIN_RELOAD_APPLIED config=%s content=%s users=%d cadastro_db=%s", self.app_context.config_path, active_content_path, len(users), self.app_context.args.cadastro_db)
        return_to = form.get("return_to", ["/users"])[0]
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = "/users"
        if return_to == "/content":
            active_name = content_file_path(getattr(self.app_context.args, "content_dir", DEFAULT_CONTENT_DIR)).name
            suffix = "?reloaded=1"
            if active_name:
                suffix += "&content_file=" + urllib.parse.quote(active_name)
            return self._redirect("/content" + suffix)
        label = "Voltar"
        if return_to == "/settings":
            label = "Voltar às configurações"
        elif return_to == "/users":
            label = "Voltar aos usuários"
        body = f"<p class='ok'>Configuração e conteúdo recarregados. Usuários encontrados: {len(users)}.</p><p class='muted'>Se foram alteradas configurações avançadas, como host, porta, transporte, porta COM ou parâmetros estruturais do servidor, o script deverá ser reiniciado.</p><p><a href='{html.escape(return_to)}'>{html.escape(label)}</a></p>"
        return self._send(200, html_page("Servidor recarregado", body))

