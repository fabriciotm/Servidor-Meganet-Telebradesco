#!/usr/bin/env python3
"""Administrative dashboard controller."""

from __future__ import annotations

import datetime as _dt
import html
import pathlib

from ..meganet import _mailbox_rows, _read_cadastro_users
from ..page import html_page
from ...config import _bind_display_host
from ...meganet.accounts import DEFAULT_SESSION_OUTPUT_DIR
from ...meganet.mail import MailStore
from ...runtime.configuration import normalize_service_transport as _normalize_service_transport
from ...telebradesco.accounts import (
    DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE, DEFAULT_TELEBRADESCO_ACCOUNTS_FILE,
    TelebradescoAccountsError, read_telebradesco_accounts,
)

class DashboardControllerMixin:
    """Dashboard route controller mixin."""

    def _get_dashboard(self, parsed, current_user=None, *, meganet_only: bool = False) -> None:
        path = '/meganet' if meganet_only else '/'

        # Painel geral e atalho /meganet usam os mesmos indicadores.
        # Carregue sempre os dados antes de montar o HTML para evitar
        # variáveis locais não inicializadas quando a rota for "/".
        users = _read_cadastro_users(pathlib.Path(self.app_context.args.cadastro_db))
        mail_status_note = ""
        try:
            mail_rows = _mailbox_rows(
                MailStore(
                    pathlib.Path(self.app_context.args.mail_store_dir),
                    getattr(self.app_context.args, "mail_inbox", None),
                    mail_domain=self.app_context.args.mail_domain,
                    admin_email=self.app_context.args.admin_email,
                )
            )
        except OSError:
            mail_rows = []
            mail_status_note = "<p class='warn'>Correio MegaNet indisponível</p>"
        runtime = getattr(self.app_context.args, "_service_runtime", None)
        active_connections = runtime.connection_count() if runtime else 0
        persisted_meganet_accesses = 0
        for rec in users.values():
            if isinstance(rec, dict):
                try:
                    persisted_meganet_accesses += int(rec.get("total_sessions") or 0)
                except (TypeError, ValueError):
                    pass
        tele_accounts_count = 0
        tele_blocked_count = 0
        persisted_telebradesco_accesses = 0
        tele_status_note = ""
        try:
            tele_store = read_telebradesco_accounts(
                pathlib.Path(getattr(self.app_context.args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE)),
                accounts_content_file=pathlib.Path(getattr(self.app_context.args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE)),
                create_if_missing=True,
            )
            tele_accounts = tele_store.get("accounts", {}) if isinstance(tele_store.get("accounts"), dict) else {}
            tele_accounts_count = len(tele_accounts)
            tele_blocked_count = sum(1 for item in tele_accounts.values() if isinstance(item, dict) and item.get("bloqueada"))
            for item in tele_accounts.values():
                if isinstance(item, dict):
                    try:
                        persisted_telebradesco_accesses += int(item.get("total_accesses") or item.get("total_sessions") or 0)
                    except (TypeError, ValueError):
                        pass
        except TelebradescoAccountsError:
            tele_status_note = "<p class='warn'>Banco de contas indisponível</p>"

        mega_mode = _normalize_service_transport(getattr(self.app_context.args, 'meganet_transport', 'tcp'))
        tele_mode = _normalize_service_transport(getattr(self.app_context.args, 'telebradesco_transport', 'tcp'))
        simultaneous = int(mega_mode != 'off') + int(tele_mode != 'off')
        mega_endpoint = f"{getattr(self.app_context.args,'meganet_host','127.0.0.1')}:{getattr(self.app_context.args,'meganet_port',19777)}"
        tele_endpoint = f"{getattr(self.app_context.args,'telebradesco_host','127.0.0.1')}:{getattr(self.app_context.args,'telebradesco_port',19779)}"
        raw_snapshot = runtime.dashboard_snapshot() if runtime else {}

        def _safe_int(value: object) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        snapshot_source = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        raw_connections = snapshot_source.get("connections", {})
        raw_connections = raw_connections if isinstance(raw_connections, dict) else {}
        raw_traffic = snapshot_source.get("traffic", {})
        raw_traffic = raw_traffic if isinstance(raw_traffic, dict) else {}
        raw_mega_traffic = raw_traffic.get("meganet", {})
        raw_mega_traffic = raw_mega_traffic if isinstance(raw_mega_traffic, dict) else {}
        raw_tele_traffic = raw_traffic.get("telebradesco", {})
        raw_tele_traffic = raw_tele_traffic if isinstance(raw_tele_traffic, dict) else {}
        raw_access_counts = snapshot_source.get("access_counts", {})
        raw_access_counts = raw_access_counts if isinstance(raw_access_counts, dict) else {}
        raw_buckets = snapshot_source.get("buckets", [])
        raw_buckets = raw_buckets if isinstance(raw_buckets, list) else []
        raw_accesses = snapshot_source.get("recent_accesses", [])
        raw_accesses = raw_accesses if isinstance(raw_accesses, list) else []
        snapshot = {
            "connections": {
                "total": _safe_int(raw_connections.get("total")),
                "meganet": _safe_int(raw_connections.get("meganet")),
                "telebradesco": _safe_int(raw_connections.get("telebradesco")),
            },
            "traffic": {
                "meganet": {"tx": _safe_int(raw_mega_traffic.get("tx")), "rx": _safe_int(raw_mega_traffic.get("rx"))},
                "telebradesco": {"tx": _safe_int(raw_tele_traffic.get("tx")), "rx": _safe_int(raw_tele_traffic.get("rx"))},
            },
            "access_counts": {
                "total": _safe_int(raw_access_counts.get("total")),
                "meganet": _safe_int(raw_access_counts.get("meganet")),
                "telebradesco": _safe_int(raw_access_counts.get("telebradesco")),
            },
            "buckets": [item if isinstance(item, dict) else {} for item in raw_buckets],
            "recent_accesses": [item for item in raw_accesses if isinstance(item, dict)],
        }

        def _format_bytes(value: int) -> str:
            value = _safe_int(value)
            if value >= 1024 * 1024:
                return f"{value / (1024 * 1024):.2f} MB"
            if value >= 1024:
                return f"{value / 1024:.2f} KB"
            return f"{value} B"

        def _format_bytes_short(value: int) -> str:
            value = _safe_int(value)
            if value >= 1024 * 1024:
                return f"{value / (1024 * 1024):.1f} MB"
            if value >= 1024:
                return f"{value / 1024:.0f} KB"
            return f"{value} B"

        def _nice_scale(value: int) -> int:
            value = max(1, _safe_int(value))
            base = 1
            while base * 10 < value:
                base *= 10
            for multiplier in (1, 2, 5, 10):
                candidate = base * multiplier
                if value <= candidate:
                    return candidate
            return base * 10

        def _transport_detail(prefix: str, mode: str) -> str:
            parts: list[str] = []
            if mode in {"tcp", "both"}:
                host = _bind_display_host(getattr(self.app_context.args, prefix + "_host", "127.0.0.1"))
                port = _safe_int(getattr(self.app_context.args, prefix + "_port", 0))
                parts.append(f"TCP {host}:{port}")
            if mode in {"serial", "both"}:
                serial_port = str(getattr(self.app_context.args, prefix + "_serial_port", "") or "COM não definida")
                serial_baud = _safe_int(getattr(self.app_context.args, prefix + "_serial_baud", 115200)) or 115200
                parts.append(f"{serial_port} @ {serial_baud}")
            return " · ".join(parts) if parts else "Transporte desabilitado"

        def _mode_label(mode: str) -> str:
            return {"tcp": "TCP", "serial": "COM", "both": "TCP+COM", "off": "OFF"}.get(mode, mode.upper())

        def _service_label(service: object) -> str:
            return "Telebradesco" if str(service) == "telebradesco" else "MegaNet"

        if path == "/meganet":
            connections = snapshot.get("connections", {})
            mega_traffic = snapshot.get("traffic", {}).get("meganet", {})
            recent_meganet = [
                item for item in snapshot.get("recent_accesses", [])
                if str(item.get("service", "")) == "meganet"
            ]

            total_user_sessions = 0
            for rec in users.values():
                if isinstance(rec, dict):
                    try:
                        total_user_sessions += int(rec.get("total_sessions") or 0)
                    except (TypeError, ValueError):
                        pass

            mailbox_count = len(mail_rows)
            mailbox_messages = sum(int(row[2] or 0) for row in mail_rows)
            output_path = pathlib.Path(getattr(self.app_context.args, "output_dir", DEFAULT_SESSION_OUTPUT_DIR))
            session_log_count = 0
            last_session_log = ""
            try:
                session_files = sorted(
                    [p for p in output_path.glob("meganet_sessions_*.json") if p.is_file()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                session_log_count = len(session_files)
                if session_files:
                    last_session_log = _dt.datetime.fromtimestamp(session_files[0].stat().st_mtime).strftime("%d/%m/%Y %H:%M:%S")
            except OSError:
                session_log_count = 0
                last_session_log = ""

            if recent_meganet:
                recent_rows = []
                for item in recent_meganet:
                    identity = str(item.get("identity") or "").strip() or "Usuário ainda não identificado"
                    recent_rows.append(
                        "<div class='access-row'>"
                        f"<span>{html.escape(str(item.get('time',''))[-8:])}</span>"
                        "<span><span class='access-service'>MegaNet</span></span>"
                        f"<span><span class='access-peer'>{html.escape(str(item.get('peer','unknown')))}</span><span class='access-identity'>{html.escape(identity)}</span></span>"
                        f"<span class='muted'>{html.escape(str(item.get('event','Acesso')))}</span>"
                        "</div>"
                    )
                history_html = (
                    "<div class='access-table'>"
                    "<div class='access-head'><span>Horário</span><span>Serviço</span><span>Origem e usuário</span><span>Evento</span></div>"
                    + "".join(recent_rows) +
                    "</div>"
                )
            else:
                history_html = "<div class='empty-state'>Nenhuma conexão MegaNet registrada desde a inicialização do servidor.</div>"

            log_status = "ativado" if bool(getattr(self.app_context.args, "enable_session_logs", False)) else "desativado"
            last_log_html = f"<small>Último registro diário: {html.escape(last_session_log)}</small>" if last_session_log else "<small>Nenhum histórico diário encontrado</small>"

            body = f"""
            {mail_status_note}
            <section class='dashboard-status-grid'>
              <article class='dashboard-status-card'>
                <div class='dashboard-status-top'><h3>Usuários MegaNet</h3><span class='status-badge'>{len(users)}</span></div>
                <strong>{len(users)}</strong>
                <p>Total de usuários cadastrados no banco atual.</p>
                <small>{total_user_sessions} sessão(ões) registradas nos cadastros</small>
              </article>
              <article class='dashboard-status-card'>
                <div class='dashboard-status-top'><h3>Sessões ativas</h3><span class='status-badge'>{connections.get('meganet',0)}</span></div>
                <strong>{connections.get('meganet',0)}</strong>
                <p>Conexões MegaNet em andamento.</p>
                <small>{html.escape(_transport_detail('meganet', mega_mode))}</small>
              </article>
              <article class='dashboard-status-card neutral'>
                <div class='dashboard-status-top'><h3>Correio</h3><span class='status-badge'>{mailbox_count}</span></div>
                <strong>{mailbox_count}</strong>
                <p>Caixas postais encontradas.</p>
                <small>{mailbox_messages} mensagem(ns) armazenada(s)</small>
              </article>
              <article class='dashboard-status-card neutral'>
                <div class='dashboard-status-top'><h3>Histórico salvo</h3><span class='status-badge'>{session_log_count}</span></div>
                <strong>{session_log_count}</strong>
                <p>Dias com histórico MegaNet; registro {html.escape(log_status)}.</p>
                {last_log_html}
              </article>
            </section>

            <section class='dashboard-main-grid'>
              <article class='traffic-total-card'>
                <div><h2>Tráfego MegaNet</h2><p class='muted'>Acumulado no histórico diário persistente.</p></div>
                <section class='traffic-service-box'>
                  <h3>MegaNet</h3>
                  <div class='traffic-pair'>
                    <div class='metric'><strong>{_format_bytes(mega_traffic.get('tx',0))}</strong><span>TX enviado</span></div>
                    <div class='metric'><strong>{_format_bytes(mega_traffic.get('rx',0))}</strong><span>RX recebido</span></div>
                  </div>
                </section>
              </article>
              <article class='traffic-total-card'>
                <div><h2>Administração MegaNet</h2><p class='muted'>Atalhos para as principais áreas de cadastro e conteúdo.</p></div>
                <section class='traffic-service-box'>
                  <h3>Ações rápidas</h3>
                  <div class='service-actions'>
                    <a class='button secondary' href='/users'>Usuários</a>
                    <a class='button secondary' href='/mailboxes'>Correio</a>
                    <a class='button secondary' href='/content'>Conteúdo</a>
                    <a class='button secondary' href='/access-code'>Gerador de código</a>
                  </div>
                </section>
              </article>
              <article class='recent-access-card'>
                <div class='section-header'><div><h2>Histórico recente de conexões MegaNet</h2><p class='muted'>Endereço de origem junto ao usuário identificado quando disponível.</p></div></div>
                <div class='access-list'>{history_html}</div>
              </article>
            </section>"""
            return self._send(200, html_page("Resumo MegaNet", body))

        buckets = list(snapshot.get("buckets", []))[-12:]
        while len(buckets) < 12:
            buckets.insert(0, {
                "label": "--:--", "meganet_tx": 0, "meganet_rx": 0,
                "telebradesco_tx": 0, "telebradesco_rx": 0,
            })
        mega_tx_values = [_safe_int(item.get("meganet_tx", 0)) for item in buckets]
        mega_rx_values = [_safe_int(item.get("meganet_rx", 0)) for item in buckets]
        tele_tx_values = [_safe_int(item.get("telebradesco_tx", 0)) for item in buckets]
        tele_rx_values = [_safe_int(item.get("telebradesco_rx", 0)) for item in buckets]
        max_value = max([1] + mega_tx_values + mega_rx_values + tele_tx_values + tele_rx_values)
        max_scale = _nice_scale(max_value)
        width, height = 1280, 460
        left, right, top, bottom = 82, 34, 32, 58
        plot_w, plot_h = width - left - right, height - top - bottom

        def _polyline(values: list[int]) -> str:
            points = []
            for index, value in enumerate(values):
                x = left + (plot_w * index / max(1, len(values) - 1))
                y = top + plot_h - (plot_h * value / max_scale)
                points.append(f"{x:.1f},{y:.1f}")
            return " ".join(points)

        tick_values = [int(round(max_scale * n / 4)) for n in range(5)]
        grid_lines = "".join(
            f"<line x1='{left}' y1='{top + plot_h - (plot_h * value / max_scale):.1f}' x2='{width-right}' y2='{top + plot_h - (plot_h * value / max_scale):.1f}' stroke='#e8edf3' stroke-width='1'/>"
            f"<text x='{left-12}' y='{top + plot_h - (plot_h * value / max_scale) + 4:.1f}' text-anchor='end' fill='#94a3b8' font-size='11'>{html.escape(_format_bytes_short(value))}</text>"
            for value in tick_values
        )
        label_indexes = {0, len(buckets) // 3, (2 * len(buckets)) // 3, len(buckets) - 1}
        x_labels = "".join(
            f"<text x='{left + plot_w * index / max(1, len(buckets)-1):.1f}' y='{height-16}' text-anchor='middle' fill='#94a3b8' font-size='11'>{html.escape(str(item.get('label','')))}</text>"
            for index, item in enumerate(buckets) if index in label_indexes
        )
        chart_svg = f"""<svg class='traffic-svg' viewBox='0 0 {width} {height}' role='img' aria-label='Tráfego de bytes enviados e recebidos por serviço'>
          <rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' rx='10' fill='#ffffff'/>
          {grid_lines}
          <line x1='{left}' y1='{top + plot_h}' x2='{width-right}' y2='{top + plot_h}' stroke='#d8e0ea' stroke-width='1.4'/>
          <line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' stroke='#d8e0ea' stroke-width='1.4'/>
          <polyline fill='none' stroke='#13b16f' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round' points='{_polyline(mega_tx_values)}'/>
          <polyline fill='none' stroke='#3b82f6' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round' points='{_polyline(mega_rx_values)}'/>
          <polyline fill='none' stroke='#8b5cf6' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round' points='{_polyline(tele_tx_values)}'/>
          <polyline fill='none' stroke='#f59e0b' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round' points='{_polyline(tele_rx_values)}'/>
          {x_labels}
          <text x='{left}' y='19' fill='#64748b' font-size='12'>bytes/min</text>
        </svg>"""

        accesses = snapshot.get("recent_accesses", [])
        if accesses:
            rows: list[str] = []
            for item in accesses:
                service = str(item.get("service", ""))
                service_class = "telebradesco" if service == "telebradesco" else ""
                identity = str(item.get("identity") or "").strip()
                if not identity:
                    identity = "Usuário ainda não identificado" if service == "meganet" else "Identificação ainda não recebida"
                rows.append(
                    "<div class='access-row'>"
                    f"<span>{html.escape(str(item.get('time',''))[-8:])}</span>"
                    f"<span><span class='access-service {service_class}'>{html.escape(_service_label(service))}</span></span>"
                    f"<span><span class='access-peer'>{html.escape(str(item.get('peer','unknown')))}</span><span class='access-identity'>{html.escape(identity)}</span></span>"
                    f"<span class='muted'>{html.escape(str(item.get('event','Acesso')))}</span>"
                    "</div>"
                )
            access_html = (
                "<div class='access-table'>"
                "<div class='access-head'><span>Horário</span><span>Serviço</span><span>Origem e identificação</span><span>Evento</span></div>"
                + "".join(rows) +
                "</div>"
            )
        else:
            access_html = "<div class='empty-state'>Nenhum acesso registrado no histórico diário.</div>"

        mega_traffic = snapshot["traffic"]["meganet"]
        tele_traffic = snapshot["traffic"]["telebradesco"]
        connections = snapshot["connections"]
        access_counts = snapshot.get("access_counts", {}) if isinstance(snapshot, dict) else {}
        persisted_total_accesses = persisted_meganet_accesses + persisted_telebradesco_accesses
        body = f"""
        {tele_status_note}
        {mail_status_note}
        <section class='dashboard-status-grid'>
          <article class='dashboard-status-card'>
            <div class='dashboard-status-top'><h3>MegaNet</h3><span class='status-badge {'off' if mega_mode == 'off' else ''}'>{html.escape(_mode_label(mega_mode))}</span></div>
            <strong>{'Online' if mega_mode != 'off' else 'Desabilitado'}</strong>
            <p>{html.escape(_transport_detail('meganet', mega_mode))}</p>
            <small>{connections.get('meganet',0)} conexão(ões) ativa(s) · {persisted_meganet_accesses} acesso(s) registrado(s)</small>
          </article>
          <article class='dashboard-status-card tele'>
            <div class='dashboard-status-top'><h3>Telebradesco</h3><span class='status-badge {'off' if tele_mode == 'off' else 'tele'}'>{html.escape(_mode_label(tele_mode))}</span></div>
            <strong>{'Online' if tele_mode != 'off' else 'Desabilitado'}</strong>
            <p>{html.escape(_transport_detail('telebradesco', tele_mode))}</p>
            <small>{connections.get('telebradesco',0)} conexão(ões) ativa(s) · {persisted_telebradesco_accesses} acesso(s) registrado(s)</small>
          </article>
          <article class='dashboard-status-card neutral'>
            <div class='dashboard-status-top'><h3>Serviços habilitados</h3><span class='status-badge'>{simultaneous}/2</span></div>
            <strong>{simultaneous}</strong>
            <p>Transportes configurados por serviço.</p>
            <small>TCP e COM independentes</small>
          </article>
          <article class='dashboard-status-card neutral'>
            <div class='dashboard-status-top'><h3>Conexões atuais</h3><span class='status-badge'>{connections.get('total',0)}</span></div>
            <strong>{connections.get('total',0)}</strong>
            <p>Sessões TCP e COM em andamento.</p>
            <small>{persisted_total_accesses} acesso(s) registrado(s)</small>
          </article>
        </section>
        <section class='dashboard-main-grid'>
          <article class='traffic-chart-card'>
            <div class='section-header'>
              <div><h2>Tráfego por serviço</h2><p class='muted'>TX e RX separados para MegaNet e Telebradesco, agrupados por minuto.</p></div>
              <div class='chart-legend'><span><i class='legend-dot legend-mega-tx'></i>MegaNet TX</span><span><i class='legend-dot legend-mega-rx'></i>MegaNet RX</span><span><i class='legend-dot legend-tele-tx'></i>Telebradesco TX</span><span><i class='legend-dot legend-tele-rx'></i>Telebradesco RX</span></div>
            </div>
            {chart_svg}
            <div class='chart-scale-note'><span><b>Escala:</b> 0 a {html.escape(_format_bytes_short(max_scale))} por minuto</span><span>Janela: últimos {len(buckets)} minuto(s)</span></div>
          </article>
          <article class='traffic-total-card'>
            <div><h2>Totais de tráfego</h2><p class='muted'>Acumulado no histórico diário persistente.</p></div>
            <section class='traffic-service-box'><h3>MegaNet</h3><div class='traffic-pair'><div class='metric'><strong>{_format_bytes(mega_traffic.get('tx',0))}</strong><span>TX enviado</span></div><div class='metric'><strong>{_format_bytes(mega_traffic.get('rx',0))}</strong><span>RX recebido</span></div></div></section>
            <section class='traffic-service-box tele'><h3>Telebradesco</h3><div class='traffic-pair'><div class='metric'><strong>{_format_bytes(tele_traffic.get('tx',0))}</strong><span>TX enviado</span></div><div class='metric'><strong>{_format_bytes(tele_traffic.get('rx',0))}</strong><span>RX recebido</span></div></div></section>
          </article>
          <article class='recent-access-card'>
            <div class='section-header'><div><h2>Últimos acessos</h2><p class='muted'>Endereço de origem junto ao usuário MegaNet ou à identificação Telebradesco.</p></div></div>
            <div class='access-list'>{access_html}</div>
          </article>
        </section>"""
        return self._send(200, html_page("Painel", body))
