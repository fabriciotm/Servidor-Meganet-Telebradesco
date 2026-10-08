"""Views e formatação das páginas de Estatísticas da administração.

As consultas permanecem na camada de runtime e são injetadas como callbacks.
Dessa forma este módulo não depende do servidor monolítico nem da camada de protocolo.
"""

from __future__ import annotations

import html
import math
import urllib.parse

def _stats_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)

def _stats_format_bytes(value: object) -> str:
    try:
        n = _stats_int(value)
    except (TypeError, ValueError):
        n = 0
    units = ["B", "KB", "MB", "GB"]
    amount = float(n)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}".replace(".0 ", " ")

def _stats_duration(seconds: object) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    if hh:
        return f"{hh}h {mm:02d}m {ss:02d}s"
    if mm:
        return f"{mm}m {ss:02d}s"
    return f"{ss}s"

def _stats_line_chart(buckets: list[dict], service_filter: str) -> str:
    width, height = 980, 330
    left, right, top, bottom = 74, 24, 28, 48
    plot_w, plot_h = width - left - right, height - top - bottom
    if not buckets:
        return f"<svg class='stats-chart-svg' viewBox='0 0 {width} {height}'><text x='{width/2:.0f}' y='{height/2:.0f}' text-anchor='middle' fill='#94a3b8' font-size='16'>Sem tráfego no período selecionado</text></svg>"
    series: list[tuple[str, str, list[int]]] = []
    if service_filter in {"all", "meganet"}:
        series.append(("MegaNet TX", "#13b16f", [_stats_int(item.get("meganet_tx")) for item in buckets]))
        series.append(("MegaNet RX", "#3b82f6", [_stats_int(item.get("meganet_rx")) for item in buckets]))
    if service_filter in {"all", "telebradesco"}:
        series.append(("Telebradesco TX", "#8b5cf6", [_stats_int(item.get("telebradesco_tx")) for item in buckets]))
        series.append(("Telebradesco RX", "#f59e0b", [_stats_int(item.get("telebradesco_rx")) for item in buckets]))
    max_value = max([1] + [max(values or [0]) for _label, _color, values in series])
    max_scale = max(1, int(math.ceil(max_value / 100.0) * 100)) if max_value > 100 else max(1, max_value)
    def polyline(values: list[int]) -> str:
        points = []
        denom = max(1, len(values) - 1)
        for index, value in enumerate(values):
            x = left + plot_w * index / denom
            y = top + plot_h - (plot_h * value / max_scale)
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)
    tick_values = [int(round(max_scale * n / 4)) for n in range(5)]
    grid = "".join(
        f"<line x1='{left}' y1='{top + plot_h - (plot_h * value / max_scale):.1f}' x2='{width-right}' y2='{top + plot_h - (plot_h * value / max_scale):.1f}' stroke='#e8edf3' stroke-width='1'/>"
        f"<text x='{left-10}' y='{top + plot_h - (plot_h * value / max_scale) + 4:.1f}' text-anchor='end' fill='#94a3b8' font-size='11'>{html.escape(_stats_format_bytes(value))}</text>"
        for value in tick_values
    )
    label_indexes = {0, len(buckets)//2, len(buckets)-1}
    xlabels = "".join(
        f"<text x='{left + plot_w * i / max(1, len(buckets)-1):.1f}' y='{height-18}' text-anchor='middle' fill='#94a3b8' font-size='11'>{html.escape(str(item.get('label') or ''))}</text>"
        for i, item in enumerate(buckets) if i in label_indexes
    )
    lines = "".join(
        f"<polyline fill='none' stroke='{color}' stroke-width='3.2' stroke-linecap='round' stroke-linejoin='round' points='{polyline(values)}'/>"
        for _label, color, values in series
    )
    legend_x = left
    legend = ""
    for label, color, _values in series:
        legend += f"<circle cx='{legend_x}' cy='16' r='5' fill='{color}'/><text x='{legend_x+10}' y='20' fill='#64748b' font-size='12'>{html.escape(label)}</text>"
        legend_x += 132 if label.startswith("MegaNet") else 170
    return f"<svg class='stats-chart-svg' viewBox='0 0 {width} {height}' role='img' aria-label='Tráfego no período'>{legend}<rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' rx='10' fill='#ffffff'/>{grid}<line x1='{left}' y1='{top+plot_h}' x2='{width-right}' y2='{top+plot_h}' stroke='#d8e0ea'/><line x1='{left}' y1='{top}' x2='{left}' y2='{top+plot_h}' stroke='#d8e0ea'/>{lines}{xlabels}</svg>"

def _stats_service_label(service: object) -> str:
    return "Telebradesco" if str(service) == "telebradesco" else "MegaNet"

def _stats_service_summary(connections: list[dict], service: str) -> dict:
    rows = [item for item in connections if str(item.get("service") or "") == service]
    identities = {
        str(item.get("identity") or "").strip()
        for item in rows
        if str(item.get("identity") or "").strip()
    }
    tx = sum(_stats_int(item.get("tx_bytes")) for item in rows)
    rx = sum(_stats_int(item.get("rx_bytes")) for item in rows)
    durations = [max(0, _stats_int(item.get("duration_seconds"))) for item in rows]
    open_count = sum(1 for item in rows if not item.get("end_at") or str(item.get("status") or "").lower() == "aberta")
    return {
        "accesses": len(rows),
        "identities": len(identities),
        "tx": tx,
        "rx": rx,
        "avg_duration": int(sum(durations) / len(durations)) if durations else 0,
        "open": open_count,
    }

def _stats_service_card(service: str, summary: dict) -> str:
    tele_cls = " tele" if service == "telebradesco" else ""
    return (
        f"<div class='stats-service-card{tele_cls}'>"
        f"<h3>{html.escape(_stats_service_label(service))}</h3>"
        "<div class='stats-service-metrics'>"
        f"<div><strong>{_stats_int(summary.get('accesses'))}</strong><span>Acessos</span></div>"
        f"<div><strong>{_stats_int(summary.get('identities'))}</strong><span>Usuários identificados</span></div>"
        f"<div><strong>{html.escape(_stats_format_bytes(summary.get('tx')))}</strong><span>Transmitido (TX)</span></div>"
        f"<div><strong>{html.escape(_stats_format_bytes(summary.get('rx')))}</strong><span>Recebido (RX)</span></div>"
        f"<div><strong>{html.escape(_stats_duration(summary.get('avg_duration')))}</strong><span>Duração média</span></div>"
        f"<div><strong>{_stats_int(summary.get('open'))}</strong><span>Conexões abertas</span></div>"
        "</div></div>"
    )

def render_system_statistics(params: dict[str, list[str]], *, query_statistics, page_renderer) -> bytes:
    stats = query_statistics(params)
    date_from = stats["start_date"].strftime("%Y-%m-%d")
    date_to = stats["end_date"].strftime("%Y-%m-%d")
    time_from = stats["start_time"].strftime("%H:%M")
    time_to = stats["end_time"].strftime("%H:%M")
    service_filter = str(stats.get("service") or "all")
    origin_value = str(params.get("origin", [""])[0] if isinstance(params.get("origin"), list) else "")
    buckets = stats.get("buckets", []) if isinstance(stats.get("buckets"), list) else []
    connections = stats.get("connections", []) if isinstance(stats.get("connections"), list) else []
    events = stats.get("events", []) if isinstance(stats.get("events"), list) else []
    totals = stats.get("totals", {}) if isinstance(stats.get("totals"), dict) else {}
    mega_totals = totals.get("meganet") if isinstance(totals.get("meganet"), dict) else {}
    tele_totals = totals.get("telebradesco") if isinstance(totals.get("telebradesco"), dict) else {}
    mega_total = _stats_int(mega_totals.get("tx")) + _stats_int(mega_totals.get("rx"))
    tele_total = _stats_int(tele_totals.get("tx")) + _stats_int(tele_totals.get("rx"))
    total_bytes = mega_total + tele_total
    chart_svg = _stats_line_chart(buckets[-240:], service_filter)

    service_cards = (
        _stats_service_card("meganet", _stats_service_summary(connections, "meganet"))
        + _stats_service_card("telebradesco", _stats_service_summary(connections, "telebradesco"))
    )

    max_conn_bytes = max([1] + [_stats_int(item.get("tx_bytes")) + _stats_int(item.get("rx_bytes")) for item in connections[:80]])
    conn_rows = []
    for item in connections[:40]:
        service = str(item.get("service") or "")
        total_conn = _stats_int(item.get("tx_bytes")) + _stats_int(item.get("rx_bytes"))
        pct = min(100, int(total_conn * 100 / max_conn_bytes))
        tele_cls = "tele" if service == "telebradesco" else ""
        identity = str(item.get("identity") or "").strip() or "-"
        conn_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('start_display') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('end_display') or '-'))}</td>"
            f"<td>{html.escape(_stats_service_label(service))}</td>"
            f"<td class='identity-cell'><b>{html.escape(identity)}</b><br><small>{html.escape(str(item.get('peer') or '-'))}</small></td>"
            f"<td>{html.escape(_stats_duration(item.get('duration_seconds')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('tx_bytes')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('rx_bytes')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('avg_bps')) + '/s')}</td>"
            f"<td><div class='bar-track'><div class='bar-fill {tele_cls}' style='width:{pct}%'></div></div></td>"
            "</tr>"
        )
    event_rows = []
    for item in events[:30]:
        service = str(item.get("service") or "")
        identity = str(item.get("identity") or "").strip() or "-"
        event_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('time_display') or '-'))}</td>"
            f"<td>{html.escape(_stats_service_label(service))}</td>"
            f"<td class='identity-cell'><b>{html.escape(identity)}</b><br><small>{html.escape(str(item.get('peer') or '-'))}</small></td>"
            f"<td>{html.escape(str(item.get('event') or '-'))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('tx_bytes')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('rx_bytes')))}</td>"
            "</tr>"
        )
    body = f"""
    <div class='card'>
      <div class='section-header'>
        <div><h2>Estatísticas</h2><p class='muted'>Acessos e tráfego persistentes dos serviços MegaNet e Telebradesco.</p></div>
      </div>
      <div class='stats-tabs'>
        <a class='button' href='/system/stats'>Resumo estatístico</a>
        <a class='button secondary' href='/system/stats/history'>Histórico de acessos</a>
      </div>
      <form method='get' action='/system/stats' class='stats-filter'>
        <label>Data inicial<input type='date' name='date_from' value='{html.escape(date_from, quote=True)}'></label>
        <label>Data final<input type='date' name='date_to' value='{html.escape(date_to, quote=True)}'></label>
        <label>Hora inicial<input type='time' name='time_from' value='{html.escape(time_from, quote=True)}'></label>
        <label>Hora final<input type='time' name='time_to' value='{html.escape(time_to, quote=True)}'></label>
        <label>Serviço<select name='service'><option value='all' {'selected' if service_filter == 'all' else ''}>Todos</option><option value='meganet' {'selected' if service_filter == 'meganet' else ''}>MegaNet</option><option value='telebradesco' {'selected' if service_filter == 'telebradesco' else ''}>Telebradesco</option></select></label>
        <label class='wide'>Origem ou identificação<input name='origin' value='{html.escape(origin_value, quote=True)}' placeholder='IP, porta, usuário ou conta'></label>
        <div><button class='okbtn' type='submit'>Aplicar filtros</button></div>
      </form>
    </div>
    <div class='stats-service-grid'>{service_cards}</div>
    <div class='grid-compact'>
      <div class='metric'><strong>{len(connections)}</strong><span>acessos no filtro</span></div>
      <div class='metric'><strong>{len(events)}</strong><span>eventos registrados</span></div>
      <div class='metric'><strong>{html.escape(_stats_format_bytes(total_bytes))}</strong><span>tráfego total</span></div>
      <div class='metric'><strong>{html.escape(_stats_format_bytes(mega_total))}</strong><span>MegaNet TX+RX</span></div>
      <div class='metric'><strong>{html.escape(_stats_format_bytes(tele_total))}</strong><span>Telebradesco TX+RX</span></div>
    </div>
    <div class='card'>
      <h3>Gráfico de tráfego</h3>
      <p class='muted'>Valores agrupados por minuto, conforme os logs diários persistentes.</p>
      {chart_svg}
    </div>
    <div class='card'>
      <div class='section-header'><div><h3>Acessos recentes do filtro</h3><p class='muted'>Exibindo até 40 acessos. Para consultar todos os registros, abra o Histórico de Acessos.</p></div><a class='button secondary' href='/system/stats/history'>Ver histórico completo</a></div>
      <div class='stats-table-wrap'>
        <table class='stats-table'><thead><tr><th>Início</th><th>Fim</th><th>Serviço</th><th>Usuário / origem</th><th>Duração</th><th>TX</th><th>RX</th><th>Média</th><th>Volume</th></tr></thead><tbody>{''.join(conn_rows) if conn_rows else '<tr><td colspan="9">Nenhum acesso encontrado no filtro.</td></tr>'}</tbody></table>
      </div>
    </div>
    <div class='card'>
      <h3>Eventos recentes do filtro</h3>
      <p class='muted'>Eventos técnicos de abertura e encerramento das conexões.</p>
      <div class='stats-table-wrap'>
        <table class='stats-table'><thead><tr><th>Hora</th><th>Serviço</th><th>Usuário / origem</th><th>Evento</th><th>TX</th><th>RX</th></tr></thead><tbody>{''.join(event_rows) if event_rows else '<tr><td colspan="6">Nenhum evento encontrado no filtro.</td></tr>'}</tbody></table>
      </div>
    </div>
    """
    return page_renderer("Estatísticas", body)

def render_system_access_history(params: dict[str, list[str]], *, query_statistics, access_date_range, page_renderer) -> bytes:
    effective = {key: list(value) if isinstance(value, list) else [str(value)] for key, value in params.items()}
    earliest, latest = access_date_range()
    raw_from = str(effective.get("date_from", [""])[0] or "").strip()
    raw_to = str(effective.get("date_to", [""])[0] or "").strip()
    if not raw_from:
        effective["date_from"] = [earliest.strftime("%Y-%m-%d")]
    if not raw_to:
        effective["date_to"] = [latest.strftime("%Y-%m-%d")]

    effective["time_from"] = ["00:00"]
    effective["time_to"] = ["23:59:59"]
    effective.pop("origin", None)
    stats = query_statistics(effective)
    rows = stats.get("connections", []) if isinstance(stats.get("connections"), list) else []

    date_from = stats["start_date"].strftime("%Y-%m-%d")
    date_to = stats["end_date"].strftime("%Y-%m-%d")
    service_filter = str(stats.get("service") or "all")
    user_value = str(effective.get("user", [""])[0] if isinstance(effective.get("user"), list) else "")

    try:
        per_page = int(str(effective.get("per_page", ["100"])[0] or "100"))
    except (TypeError, ValueError):
        per_page = 100
    if per_page not in {50, 100, 200}:
        per_page = 100
    try:
        page = max(1, int(str(effective.get("page", ["1"])[0] or "1")))
    except (TypeError, ValueError):
        page = 1
    total_rows = len(rows)
    total_pages = max(1, int(math.ceil(total_rows / per_page))) if total_rows else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    page_rows = rows[offset:offset + per_page]

    unique_users = {
        str(item.get("identity") or "").strip()
        for item in rows
        if str(item.get("identity") or "").strip()
    }
    total_tx = sum(_stats_int(item.get("tx_bytes")) for item in rows)
    total_rx = sum(_stats_int(item.get("rx_bytes")) for item in rows)
    mega_count = sum(1 for item in rows if str(item.get("service") or "") == "meganet")
    tele_count = sum(1 for item in rows if str(item.get("service") or "") == "telebradesco")

    history_rows = []
    for item in page_rows:
        service = str(item.get("service") or "")
        identity = str(item.get("identity") or "").strip() or "-"
        status = str(item.get("status") or ("aberta" if not item.get("end_at") else "encerrada")).strip().lower()
        status_label = "Aberta" if status == "aberta" else "Encerrada"
        status_class = "status-open" if status == "aberta" else "status-closed"
        history_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('start_display') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('end_display') or '-'))}</td>"
            f"<td>{html.escape(_stats_service_label(service))}</td>"
            f"<td class='identity-cell'><b>{html.escape(identity)}</b></td>"
            f"<td class='origin-cell'>{html.escape(str(item.get('peer') or '-'))}</td>"
            f"<td>{html.escape(_stats_duration(item.get('duration_seconds')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('tx_bytes')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('rx_bytes')))}</td>"
            f"<td>{html.escape(_stats_format_bytes(item.get('avg_bps')) + '/s')}</td>"
            f"<td class='{status_class}'>{status_label}</td>"
            "</tr>"
        )

    def history_url(target_page: int) -> str:
        query = {
            "date_from": date_from,
            "date_to": date_to,
            "user": user_value,
            "service": service_filter,
            "per_page": str(per_page),
            "page": str(target_page),
        }
        return "/system/stats/history?" + urllib.parse.urlencode(query)

    if total_pages <= 7:
        visible_pages = list(range(1, total_pages + 1))
    else:
        visible_pages = sorted({1, total_pages, page - 2, page - 1, page, page + 1, page + 2})
        visible_pages = [n for n in visible_pages if 1 <= n <= total_pages]
    page_links = []
    for number in visible_pages:
        cls = "button" if number == page else "button secondary"
        page_links.append(f"<a class='{cls}' href='{html.escape(history_url(number), quote=True)}'>{number}</a>")
    prev_link = (
        f"<a class='button secondary' href='{html.escape(history_url(page - 1), quote=True)}'>Anterior</a>"
        if page > 1 else ""
    )
    next_link = (
        f"<a class='button secondary' href='{html.escape(history_url(page + 1), quote=True)}'>Próxima</a>"
        if page < total_pages else ""
    )

    first_shown = offset + 1 if total_rows else 0
    last_shown = min(offset + len(page_rows), total_rows)
    body = f"""
    <div class='card'>
      <div class='section-header'>
        <div><h2>Histórico de Acessos</h2><p class='muted'>Histórico persistente completo das conexões MegaNet e Telebradesco.</p></div>
      </div>
      <div class='stats-tabs'>
        <a class='button secondary' href='/system/stats'>Resumo estatístico</a>
        <a class='button' href='/system/stats/history'>Histórico de acessos</a>
      </div>
      <form method='get' action='/system/stats/history' class='stats-filter'>
        <label>Data inicial<input type='date' name='date_from' value='{html.escape(date_from, quote=True)}'></label>
        <label>Data final<input type='date' name='date_to' value='{html.escape(date_to, quote=True)}'></label>
        <label class='wide'>Usuário / identificação<input name='user' value='{html.escape(user_value, quote=True)}' placeholder='Nome MegaNet ou identificação Telebradesco'></label>
        <label>Serviço<select name='service'><option value='all' {'selected' if service_filter == 'all' else ''}>Todos</option><option value='meganet' {'selected' if service_filter == 'meganet' else ''}>MegaNet</option><option value='telebradesco' {'selected' if service_filter == 'telebradesco' else ''}>Telebradesco</option></select></label>
        <label>Registros por página<select name='per_page'><option value='50' {'selected' if per_page == 50 else ''}>50</option><option value='100' {'selected' if per_page == 100 else ''}>100</option><option value='200' {'selected' if per_page == 200 else ''}>200</option></select></label>
        <div><button class='okbtn' type='submit'>Filtrar histórico</button></div>
      </form>
    </div>
    <div class='grid-compact'>
      <div class='metric'><strong>{total_rows}</strong><span>acessos encontrados</span></div>
      <div class='metric'><strong>{mega_count}</strong><span>acessos MegaNet</span></div>
      <div class='metric'><strong>{tele_count}</strong><span>acessos Telebradesco</span></div>
      <div class='metric'><strong>{len(unique_users)}</strong><span>usuários identificados</span></div>
      <div class='metric'><strong>{html.escape(_stats_format_bytes(total_tx))}</strong><span>TX dos acessos</span></div>
      <div class='metric'><strong>{html.escape(_stats_format_bytes(total_rx))}</strong><span>RX dos acessos</span></div>
    </div>
    <div class='card'>
      <div class='section-header'><div><h3>Registros</h3><p class='muted'>Mostrando {first_shown}–{last_shown} de {total_rows} acesso(s), em ordem do mais recente para o mais antigo.</p></div></div>
      <div class='stats-table-wrap'>
        <table class='stats-table'><thead><tr><th>Início</th><th>Fim</th><th>Serviço</th><th>Usuário / identificação</th><th>Origem</th><th>Duração</th><th>TX</th><th>RX</th><th>Média</th><th>Status</th></tr></thead><tbody>{''.join(history_rows) if history_rows else '<tr><td colspan="10">Nenhum acesso encontrado para os filtros informados.</td></tr>'}</tbody></table>
      </div>
      <div class='stats-pagination'>
        <span class='muted'>Página {page} de {total_pages}</span>
        <div class='pages'>{prev_link}{''.join(page_links)}{next_link}</div>
      </div>
    </div>
    """
    return page_renderer("Histórico de Acessos", body)
