#!/usr/bin/env python3
"""Shared page shell, assets and CSRF helpers for the admin UI."""

from __future__ import annotations

import html
import logging
import pathlib
import re
import secrets

from .auth import (
    get_current_user as _get_admin_web_current_user, initials as _admin_initials,
    normalize_role as _normalize_admin_role, role_label as _admin_role_label,
)
from ..meganet.content import (
    MAX_CONTENT_GROUP_SAFE_BYTES, MAX_CONTENT_RECORD_LINES,
    MAX_CONTENT_RECORD_TOTAL_LINES, MAX_MESSAGE_LINE_LEN,
)

def _load_side_brand_logo() -> bytes:
    candidates = [
        pathlib.Path(__file__).resolve().parent.parent / "assets" / "logo_side_brand.png",
        pathlib.Path(__file__).resolve().parent.parent / "logo_side_brand.png",
        pathlib.Path.cwd() / "logo_side_brand.png",
    ]
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path.read_bytes()
        except OSError as exc:
            logging.warning("SIDE_BRAND_LOGO_READ_FAILED path=%s error=%s", path, exc)
    logging.warning("SIDE_BRAND_LOGO_NOT_FOUND candidates=%s", ",".join(str(p) for p in candidates))
    return b""



_ADMIN_CSRF_TOKEN = secrets.token_urlsafe(32)
_SIDE_BRAND_LOGO_PNG = _load_side_brand_logo()



def _inject_admin_csrf(body: str) -> str:
    hidden = f"<input type='hidden' name='_csrf' value='{html.escape(_ADMIN_CSRF_TOKEN, quote=True)}'>"
    return re.sub(
        r"(<form\b[^>]*\bmethod=[\"\']post[\"\'][^>]*>)",
        lambda match: match.group(1) + hidden,
        body,
        flags=re.IGNORECASE,
    )

def html_page(title: str, body: str) -> bytes:
    body = _inject_admin_csrf(body)
    css = r"""
    :root{--bg:#f4f6fb;--surface:#ffffff;--surface2:#f8fafc;--line:#e5eaf2;--text:#1f2937;--muted:#6b7280;--green:#13b16f;--green2:#0c9b61;--green-soft:#e8f8f1;--blue:#3b82f6;--amber:#f59e0b;--red:#ef4444;--purple:#8b5cf6;--shadow:0 12px 36px rgba(18,38,63,.08);--radius:18px}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif}a{color:#0f9d66}
    body{background:linear-gradient(180deg,#f6f8fb 0,#eef3f9 100%)}
    .layout{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:100vh}
    .sidebar{position:sticky;top:0;height:100vh;background:#fff;border-right:1px solid var(--line);padding:0 18px 24px;overflow:auto}
    .side-brand{display:flex;align-items:center;justify-content:center;margin:0 -18px 18px;padding:18px 14px;min-height:122px;background:transparent;color:#fff;text-decoration:none;border-bottom:1px solid transparent}
    .side-brand-logo{display:block;width:100%;max-width:218px;height:auto;object-fit:contain}
    .nav-group{margin:18px 0 8px;padding:0 8px;color:#9aa6b2;font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}
    .nav-link{display:flex;align-items:center;gap:12px;padding:12px 14px;margin:4px 0;border-radius:14px;color:#334155;text-decoration:none;font-weight:650;border:1px solid transparent}
    .nav-link:hover{background:#f1f5f9;border-color:#e2e8f0;color:#0f172a}.nav-link.active{background:#e9f8f2;border-color:#c6efdd;color:#067647;box-shadow:inset 0 0 0 1px rgba(19,177,111,.08)}
    .nav-icon{width:28px;height:28px;border-radius:10px;display:inline-flex;align-items:center;justify-content:center;background:#f3f4f6;color:#64748b;font-size:14px;flex:none}.nav-link.active .nav-icon{background:var(--green);color:#fff}
    .content{min-width:0;display:flex;flex-direction:column}
    .topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:16px;padding:14px 24px;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
    .menu-chip{width:42px;height:42px;border-radius:12px;border:1px solid var(--line);background:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;color:#4b5563}
    .search-wrap{flex:1;max-width:440px;position:relative}.search-wrap input{width:100%;height:44px;padding:0 16px 0 42px;border-radius:14px;border:1px solid #d8e0ea;background:#fff;color:var(--text);outline:none;font-size:14px}.search-wrap:before{content:"🔎";position:absolute;left:14px;top:10px;font-size:16px;opacity:.65}
    .top-actions{margin-left:auto;display:flex;align-items:center;gap:10px}.logout-form{margin:0}.small-button{min-height:38px;padding:0 13px}.top-pill,.top-icon{display:inline-flex;align-items:center;justify-content:center;height:40px;min-width:40px;padding:0 12px;border-radius:999px;background:#fff;border:1px solid var(--line);color:#475569;font-size:14px}.top-pill{gap:8px;padding:0 14px;font-weight:700}.status-dot{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px rgba(19,177,111,.14)}
    .user-chip{display:flex;align-items:center;gap:10px;padding:6px 8px 6px 6px;border-radius:999px;background:#fff;border:1px solid var(--line)}.user-chip .avatar{width:34px;height:34px;border-radius:50%;background:var(--green);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800}.user-chip small{display:block;color:#6b7280;font-size:11px}.user-chip strong{display:block;font-size:13px;color:#0f172a}
    .page{padding:24px}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:18px}.page-head h1{margin:0;font-size:22px;letter-spacing:-.02em;color:#0f172a}.page-head p{margin:6px 0 0;color:#6b7280;font-size:14px}
    .field-guide{margin-bottom:18px;padding:18px 20px;background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
    .field-guide h2{margin:0 0 8px;font-size:17px;color:#0f172a}.field-guide p{margin:0 0 12px;color:#475569;line-height:1.55}.field-guide ul{margin:0;padding-left:18px;color:#475569;line-height:1.65}.field-guide code{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:2px 6px;color:#0f172a}
    .card,.content-item,.mini-card,details.menu-group,details.subgroup,details.category,details.settings-group{background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
    .card{padding:18px;margin:14px 0}.card h2,.card h3{margin:0 0 10px;color:#0f172a}.card-flat{box-shadow:none}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.content-grid-four{grid-template-columns:repeat(4,minmax(0,1fr))}.content-grid-three{grid-template-columns:repeat(3,minmax(0,1fr))}.grid-compact{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}.service-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
    .service-card{padding:0;overflow:hidden}.service-card .service-title{padding:18px 18px 0 18px}.service-card .service-body{padding:0 18px 18px 18px}.service-card{border-top:4px solid var(--green)}.service-card.tele{border-top-color:#8b5cf6}
    .section-header,.service-title,.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.toolbar-spacer{flex:1}
    .metric{padding:16px;border-radius:16px;border:1px solid #ebeff5;background:#fbfcfe;box-shadow:none}.metric strong{display:block;font-size:28px;line-height:1.1;color:#0f172a}.metric span,.muted{color:#6b7280}.metric small{display:block;margin-top:4px;color:#94a3b8}.stat-icon{width:48px;height:48px;border-radius:14px;background:var(--green-soft);color:var(--green);display:flex;align-items:center;justify-content:center;font-size:22px}
    .kpi-card{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px;border-radius:18px;background:#fff;border:1px solid var(--line);box-shadow:var(--shadow)}.kpi-card h3{margin:0 0 6px;font-size:14px;color:#6b7280;font-weight:700}.kpi-card strong{display:block;font-size:20px;color:#0f172a}.kpi-card .delta{display:block;margin-top:4px;font-size:12px;color:#0f9d66;font-weight:700}
    .service-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.pill{display:inline-flex;align-items:center;border:1px solid #d7e3dd;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:800;background:#f8fafc}.ok-pill{color:#0f9d66;background:#eefbf5;border-color:#cdeedc}.warn-pill{color:#b54708;background:#fff7ed;border-color:#fed7aa}.warn{color:#b91c1c}.ok{color:#0f9d66}
    button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:0 16px;border-radius:12px;border:1px solid transparent;background:linear-gradient(135deg,var(--green),var(--green2));color:#fff!important;font-weight:800;text-decoration:none;cursor:pointer;box-shadow:none}.button.secondary,button.secondary{background:#fff;color:#0f172a!important;border-color:#d7e0ea}.button.danger,button.warnbtn{background:linear-gradient(135deg,#ef4444,#dc2626)}.button.okbtn,button.okbtn{background:linear-gradient(135deg,#10b981,#059669)}.button.bluebtn,button.bluebtn{background:linear-gradient(135deg,#3b82f6,#2563eb);color:#fff!important}.admin-user-form{margin:0}.admin-user-actions{display:flex;align-items:center;justify-content:flex-start;gap:10px;flex-wrap:wrap;margin-top:14px}.admin-user-table input,.admin-user-table select{margin-top:0}.admin-user-table .password-cell{min-width:180px}.row-check{display:inline-flex;align-items:center;gap:10px;margin:0;color:#334155}.row-check input{width:auto;margin:0}.admin-user-table td{vertical-align:middle}.admin-user-table th:first-child,.admin-user-table td:first-child{min-width:190px}.button-row-left{display:flex;justify-content:flex-start;align-items:center;gap:10px;flex-wrap:wrap}.action-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.inline-action-form{display:inline-flex;margin:0}.tele-account-actions{justify-content:flex-start}.tele-auth-header h3{margin:0}
    input,select,textarea{width:100%;margin-top:7px;padding:11px 13px;border-radius:12px;border:1px solid #d7e0ea;background:#fff;color:var(--text);outline:none}input:focus,select:focus,textarea:focus{border-color:#8ad7b4;box-shadow:0 0 0 4px rgba(19,177,111,.12)}label{display:block;margin:10px 0;font-size:14px;font-weight:700;color:#334155}.inline label{display:inline-flex;align-items:center;gap:8px;margin-right:14px}.inline input{width:auto;margin:0}textarea{min-height:110px;font-family:ui-monospace,Consolas,monospace}
    table{width:100%;border-collapse:separate;border-spacing:0;margin:14px 0;border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#fff;box-shadow:var(--shadow)}th,td{padding:12px 13px;border-bottom:1px solid #edf2f7;text-align:left;vertical-align:top;color:#334155}th{background:#f8fafc;color:#64748b;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:last-child td{border-bottom:0}tbody tr:hover{background:#f9fbfd}
    details.menu-group,details.subgroup,details.category,details.settings-group{margin:12px 0;overflow:hidden}details>summary{cursor:pointer;padding:14px 16px;background:#f8fafc;font-weight:800;color:#0f172a;list-style:none}details summary::-webkit-details-marker{display:none}details>summary:after{content:"+";float:right}details[open]>summary:after{content:"−"}.content-item,.mini-card{padding:12px;margin:10px;border-radius:14px}.raw-json{min-height:460px}.advanced-settings{display:none}.advanced-settings.show{display:block}.help,.breadcrumb{font-size:13px;color:#6b7280}.sibling-list{display:flex;flex-direction:column;gap:12px}.section-title{margin-top:18px;padding-top:6px;border-top:1px solid #eef2f7}.code-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.mail-preview{white-space:pre-wrap;padding:14px;border:1px solid #e6ecf2;border-radius:14px;background:#fafbfd}.transport-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.tele-last-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.ascii-preview{white-space:pre;overflow:auto;background:#fbfcfe;border:1px solid #e5ebf2;border-radius:16px;padding:14px;color:#334155}.small{font-size:12px;color:#6b7280}.code,code{font-family:ui-monospace,Consolas,monospace;background:#f2f6fa;color:#0f172a;padding:2px 6px;border-radius:6px}
    .dashboard-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:18px}.dashboard-feature{display:grid;grid-template-columns:1.35fr .85fr;gap:16px;align-items:start}.chart-card{padding:18px;min-height:360px}.chart-area{margin-top:18px;height:220px;border-radius:16px;background:linear-gradient(180deg,#f8fcfa,#eef8f4);border:1px solid #e4efe9;position:relative;overflow:hidden}.chart-area svg{position:absolute;inset:0;width:100%;height:100%}.mini-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:16px}.mini-chart{height:140px;border-radius:16px;background:#f9fbfd;border:1px solid #edf2f7;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:13px}
    .footer-links{margin-top:18px;padding:12px 16px;border-radius:999px;background:linear-gradient(135deg,var(--green),var(--green2));display:flex;flex-wrap:wrap;gap:12px;justify-content:center}.footer-links a{color:#fff;text-decoration:none;font-weight:800;padding:4px 8px}
    .dashboard-page{max-width:1840px;width:100%;margin:0 auto}.dashboard-page .page-head{margin-bottom:20px}.dashboard-status-grid{display:grid;grid-template-columns:repeat(4,minmax(240px,1fr));gap:20px;margin-bottom:22px}.dashboard-status-card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:22px;min-height:148px;box-shadow:var(--shadow);border-top:5px solid var(--green);display:flex;flex-direction:column;justify-content:space-between}.dashboard-status-card.tele{border-top-color:var(--purple)}.dashboard-status-card.neutral{border-top-color:var(--blue)}.dashboard-status-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.dashboard-status-card h3{margin:0;font-size:14px;color:#64748b;text-transform:uppercase;letter-spacing:.04em}.dashboard-status-card strong{display:block;font-size:30px;line-height:1.05;color:#0f172a}.dashboard-status-card p{margin:10px 0 0;color:#6b7280;font-size:13px;line-height:1.45}.dashboard-status-card small{display:block;margin-top:8px;color:#94a3b8;font-size:12px}.status-badge{display:inline-flex;align-items:center;justify-content:center;min-width:58px;padding:6px 10px;border-radius:999px;background:#eefbf5;border:1px solid #cdeedc;color:#0f9d66;font-weight:900;font-size:11px;letter-spacing:.05em}.status-badge.off{background:#f8fafc;border-color:#e2e8f0;color:#64748b}.status-badge.tele{background:#f3efff;border-color:#ddd6fe;color:#7c3aed}.dashboard-main-grid{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(340px,.8fr);gap:20px;align-items:stretch}.traffic-chart-card,.traffic-total-card,.recent-access-card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:24px;box-shadow:var(--shadow)}.traffic-chart-card h2,.traffic-total-card h2,.recent-access-card h2{margin:0;font-size:20px}.traffic-total-card{display:flex;flex-direction:column;gap:16px}.recent-access-card{grid-column:1/-1}.chart-legend{display:flex;gap:16px 22px;align-items:center;flex-wrap:wrap;color:#64748b;font-size:12px}.legend-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}.legend-mega-tx{background:#13b16f}.legend-mega-rx{background:#3b82f6}.legend-tele-tx{background:#8b5cf6}.legend-tele-rx{background:#f59e0b}.traffic-svg{display:block;width:100%;height:520px;margin-top:18px;border-radius:18px;background:#fbfcfe;border:1px solid #edf2f7}.chart-scale-note{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;padding:12px 14px;border-radius:14px;background:#f8fafc;border:1px solid #edf2f7;color:#64748b;font-size:12px}.chart-scale-note b{color:#0f172a}.traffic-service-box{border:1px solid #e8edf3;border-radius:18px;padding:17px;background:#fbfcfe}.traffic-service-box h3{margin:0 0 14px;font-size:15px}.traffic-service-box.tele h3{color:#7c3aed}.traffic-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.traffic-pair .metric{padding:14px}.traffic-pair .metric strong{font-size:21px}.access-list{margin-top:16px}.access-table{border:1px solid var(--line);border-radius:18px;overflow:hidden;background:#fff}.access-head,.access-row{display:grid;grid-template-columns:110px 150px minmax(320px,1fr) 210px;gap:14px;align-items:center}.access-head{padding:11px 14px;background:#f8fafc;color:#64748b;font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:.06em}.access-row{padding:14px;border-top:1px solid #edf2f7;font-size:13px}.access-row:hover{background:#fbfcfe}.access-peer{font-weight:900;color:#0f172a}.access-identity{display:block;color:#475569;margin-top:3px}.access-service{display:inline-flex;align-items:center;width:max-content;padding:6px 10px;border-radius:999px;background:#eefbf5;color:#0f9d66;font-weight:900;font-size:11px;text-transform:uppercase}.access-service.telebradesco{background:#f3efff;color:#7c3aed}.empty-state{padding:38px 12px;text-align:center;color:#94a3b8;border:1px dashed #dbe4ee;border-radius:18px;background:#fbfcfe}
    .hide-small{} .desktop-only{} .mobile-only{display:none}.flash{padding:12px 14px;border-radius:14px;border:1px solid #d8ecdf;background:#eefbf5;color:#0f9d66;font-weight:700;margin:0 0 16px}.flash.warn{background:#fff7ed;border-color:#fed7aa;color:#b45309}
    .toast-container{position:fixed;top:18px;right:18px;z-index:9999;display:flex;flex-direction:column;gap:10px;width:min(390px,calc(100vw - 36px));pointer-events:none}.toast{padding:13px 15px;border-radius:15px;border:1px solid #d8e5ee;background:#fff;color:#0f172a;box-shadow:0 18px 46px rgba(15,23,42,.18);opacity:0;transform:translateY(-8px);transition:opacity .22s ease,transform .22s ease;pointer-events:auto;line-height:1.45}.toast.show{opacity:1;transform:translateY(0)}.toast.hide{opacity:0;transform:translateY(-8px)}.toast.ok{background:#eefbf5;border-color:#bfead5;color:#065f46}.toast.warn{background:#fff7ed;border-color:#fed7aa;color:#9a3412}.toast.error{background:#fff1f2;border-color:#fecdd3;color:#991b1b}.toast p{margin:0 0 4px}.toast p:last-child{margin-bottom:0}.toast b{font-weight:900}.toast .muted{color:inherit;opacity:.78}.toast-close{float:right;margin:-3px -5px 0 10px;border:0;background:transparent;color:inherit;font-size:18px;line-height:1;cursor:pointer;opacity:.7}.toast-close:hover{opacity:1}
    .theme-switch{display:inline-flex;align-items:center;gap:8px;margin:0;font-size:12px;font-weight:800;color:#475569;cursor:pointer;user-select:none}.theme-switch input{position:absolute;opacity:0;width:1px;height:1px}.theme-switch span{width:48px;height:26px;border-radius:999px;background:#e2e8f0;border:1px solid #cbd5e1;position:relative;transition:.18s}.theme-switch span:before{content:"";position:absolute;width:20px;height:20px;left:2px;top:2px;border-radius:50%;background:#fff;box-shadow:0 1px 4px rgba(15,23,42,.22);transition:.18s}.theme-switch input:checked+span{background:#13b16f;border-color:#13b16f}.theme-switch input:checked+span:before{transform:translateX(22px)}.theme-switch em{font-style:normal;white-space:nowrap}
    :root[data-theme='dark']{--bg:#0f172a;--surface:#111827;--surface2:#1f2937;--line:#334155;--text:#e5e7eb;--muted:#94a3b8;--green-soft:rgba(19,177,111,.16);--shadow:0 16px 44px rgba(0,0,0,.30)}
    :root[data-theme='dark'] body{background:linear-gradient(180deg,#0f172a 0,#111827 100%);color:var(--text)}:root[data-theme='dark'] .sidebar,:root[data-theme='dark'] .topbar,:root[data-theme='dark'] .card,:root[data-theme='dark'] .content-item,:root[data-theme='dark'] .mini-card,:root[data-theme='dark'] details.menu-group,:root[data-theme='dark'] details.subgroup,:root[data-theme='dark'] details.category,:root[data-theme='dark'] details.settings-group,:root[data-theme='dark'] .kpi-card,:root[data-theme='dark'] .dashboard-status-card,:root[data-theme='dark'] .traffic-total-card,:root[data-theme='dark'] .access-panel,:root[data-theme='dark'] .field-guide{background:#111827;border-color:#334155;color:#e5e7eb}:root[data-theme='dark'] .topbar{background:rgba(15,23,42,.92)}:root[data-theme='dark'] .sidebar{background:#0b1220}:root[data-theme='dark'] .card h2,:root[data-theme='dark'] .card h3,:root[data-theme='dark'] .page-head h1,:root[data-theme='dark'] .field-guide h2,:root[data-theme='dark'] .nav-link:hover,:root[data-theme='dark'] .user-chip strong,:root[data-theme='dark'] .metric strong,:root[data-theme='dark'] .kpi-card strong{color:#f8fafc}:root[data-theme='dark'] .muted,:root[data-theme='dark'] .metric span,:root[data-theme='dark'] .help,:root[data-theme='dark'] .breadcrumb,:root[data-theme='dark'] .small,:root[data-theme='dark'] .field-guide p,:root[data-theme='dark'] .field-guide ul{color:#94a3b8}:root[data-theme='dark'] .nav-link{color:#cbd5e1}:root[data-theme='dark'] .nav-link:hover,:root[data-theme='dark'] details>summary,:root[data-theme='dark'] th{background:#1f2937;border-color:#334155}:root[data-theme='dark'] .nav-icon,:root[data-theme='dark'] .top-pill,:root[data-theme='dark'] .top-icon,:root[data-theme='dark'] .user-chip,:root[data-theme='dark'] .metric,:root[data-theme='dark'] input,:root[data-theme='dark'] select,:root[data-theme='dark'] textarea,:root[data-theme='dark'] table,:root[data-theme='dark'] .mail-preview,:root[data-theme='dark'] .ascii-preview{background:#0f172a;border-color:#334155;color:#e5e7eb}:root[data-theme='dark'] td{border-bottom-color:#1f2937;color:#e5e7eb}:root[data-theme='dark'] tbody tr:hover{background:#182235}:root[data-theme='dark'] code,:root[data-theme='dark'] .code{background:#1f2937;border-color:#334155;color:#d1fae5}:root[data-theme='dark'] .chart-area{background:linear-gradient(180deg,#111827,#0f172a);border-color:#334155}:root[data-theme='dark'] .theme-switch{color:#cbd5e1}

    :root[data-theme='dark'] label,:root[data-theme='dark'] .row-check,:root[data-theme='dark'] .section-header h2,:root[data-theme='dark'] .section-header h3,:root[data-theme='dark'] .content-item h4,:root[data-theme='dark'] details>summary,:root[data-theme='dark'] .page-head h1,:root[data-theme='dark'] .card h4{color:#f8fafc}
    :root[data-theme='dark'] .page-head p,:root[data-theme='dark'] .muted,:root[data-theme='dark'] .metric span,:root[data-theme='dark'] .metric small,:root[data-theme='dark'] .help,:root[data-theme='dark'] .breadcrumb,:root[data-theme='dark'] .small,:root[data-theme='dark'] .field-guide p,:root[data-theme='dark'] .field-guide ul,:root[data-theme='dark'] .kpi-card h3{color:#cbd5e1}
    :root[data-theme='dark'] a{color:#86efac}:root[data-theme='dark'] input::placeholder,:root[data-theme='dark'] textarea::placeholder{color:#94a3b8}:root[data-theme='dark'] th{color:#e2e8f0}:root[data-theme='dark'] .button.secondary,:root[data-theme='dark'] button.secondary{background:#1f2937;color:#f8fafc!important;border-color:#475569}:root[data-theme='dark'] .pill{background:#1f2937;border-color:#475569;color:#e2e8f0}:root[data-theme='dark'] .ok-pill{background:rgba(16,185,129,.16);border-color:rgba(16,185,129,.38);color:#86efac}:root[data-theme='dark'] .warn-pill{background:rgba(245,158,11,.16);border-color:rgba(245,158,11,.38);color:#fcd34d}:root[data-theme='dark'] .warn{color:#fca5a5}:root[data-theme='dark'] .ok{color:#86efac}

    .content-actions{justify-content:flex-start;align-items:center}.content-actions form{margin:0}.content-actions .muted{flex-basis:100%;margin-top:2px}.content-file-selector{display:grid;grid-template-columns:minmax(260px,1fr) auto;gap:12px;align-items:end}.content-file-selector label{margin:0}.content-file-card{margin-bottom:12px}
    .stats-filter{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px;align-items:end}.stats-filter .wide{grid-column:span 2}.stats-chart-svg{display:block;width:100%;height:360px;margin-top:14px;border-radius:16px;background:#fbfcfe;border:1px solid #edf2f7}.stats-table-wrap{overflow:auto}.stats-table td,.stats-table th{white-space:nowrap}.stats-table .identity-cell{min-width:220px;white-space:normal}.stats-table .origin-cell{min-width:180px}.bar-track{height:10px;background:#eef2f7;border-radius:999px;overflow:hidden}.bar-fill{height:100%;background:#13b16f;border-radius:999px}.bar-fill.tele{background:#8b5cf6}.stats-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.stats-tabs .button{min-height:38px}.stats-service-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:14px 0}.stats-service-card{padding:18px;border-radius:18px;background:#fff;border:1px solid var(--line);border-top:4px solid var(--green);box-shadow:var(--shadow)}.stats-service-card.tele{border-top-color:var(--purple)}.stats-service-card h3{margin:0 0 12px}.stats-service-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.stats-service-metrics div{padding:10px 12px;border-radius:12px;background:#f8fafc;border:1px solid #edf2f7}.stats-service-metrics strong{display:block;font-size:19px;color:#0f172a}.stats-service-metrics span{display:block;margin-top:3px;color:#64748b;font-size:12px}.stats-pagination{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:14px 0}.stats-pagination .pages{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.status-open{color:#067647;font-weight:800}.status-closed{color:#64748b;font-weight:700}
    :root[data-theme='dark'] .stats-chart-svg{background:#0b1220!important;border-color:#475569!important}:root[data-theme='dark'] .stats-service-card{background:#111827;border-color:#334155}:root[data-theme='dark'] .stats-service-card h3,:root[data-theme='dark'] .stats-service-metrics strong{color:#f8fafc}:root[data-theme='dark'] .stats-service-metrics div{background:#0f172a;border-color:#334155}:root[data-theme='dark'] .stats-service-metrics span{color:#cbd5e1}
    :root[data-theme='dark'] .dashboard-page .page-head h1,
    :root[data-theme='dark'] .dashboard-page .page-head p,
    :root[data-theme='dark'] .traffic-chart-card h2,
    :root[data-theme='dark'] .traffic-total-card h2,
    :root[data-theme='dark'] .recent-access-card h2,
    :root[data-theme='dark'] .traffic-service-box h3,
    :root[data-theme='dark'] .dashboard-status-card strong,
    :root[data-theme='dark'] .dashboard-status-card h3,
    :root[data-theme='dark'] .access-peer{color:#f8fafc!important}
    :root[data-theme='dark'] .dashboard-status-card p,
    :root[data-theme='dark'] .dashboard-status-card small,
    :root[data-theme='dark'] .chart-legend,
    :root[data-theme='dark'] .chart-scale-note,
    :root[data-theme='dark'] .access-identity,
    :root[data-theme='dark'] .access-row .muted,
    :root[data-theme='dark'] .traffic-chart-card .muted,
    :root[data-theme='dark'] .traffic-total-card .muted,
    :root[data-theme='dark'] .recent-access-card .muted{color:#dbeafe!important}
    :root[data-theme='dark'] .traffic-chart-card,
    :root[data-theme='dark'] .recent-access-card,
    :root[data-theme='dark'] .traffic-total-card,
    :root[data-theme='dark'] .dashboard-status-card,
    :root[data-theme='dark'] .traffic-service-box,
    :root[data-theme='dark'] .access-table,
    :root[data-theme='dark'] .empty-state{background:#111827!important;border-color:#475569!important;color:#f8fafc!important}
    :root[data-theme='dark'] .traffic-svg,
    :root[data-theme='dark'] .chart-scale-note{background:#0b1220!important;border-color:#475569!important;color:#e2e8f0!important}
    :root[data-theme='dark'] .chart-scale-note b{color:#f8fafc!important}
    :root[data-theme='dark'] .access-head{background:#1e293b!important;border-color:#475569!important;color:#f8fafc!important}
    :root[data-theme='dark'] .access-row{background:#111827!important;border-top-color:#334155!important;color:#f8fafc!important}
    :root[data-theme='dark'] .access-row:hover{background:#1e293b!important}
    :root[data-theme='dark'] .status-badge{background:#064e3b!important;border-color:#10b981!important;color:#ecfdf5!important}
    :root[data-theme='dark'] .status-badge.tele{background:#4c1d95!important;border-color:#a78bfa!important;color:#f5f3ff!important}
    :root[data-theme='dark'] .status-badge.off{background:#334155!important;border-color:#64748b!important;color:#f8fafc!important}
    :root[data-theme='dark'] .nav-link.active{background:#064e3b!important;border-color:#10b981!important;color:#ecfdf5!important;box-shadow:inset 0 0 0 1px rgba(16,185,129,.34)}
    :root[data-theme='dark'] .nav-link.active:hover{background:#047857!important;color:#ffffff!important}
    :root[data-theme='dark'] .nav-link.active .nav-icon{background:#10b981!important;color:#042f2e!important}
    :root[data-theme='dark'] .nav-link:hover{background:#1e293b!important;color:#f8fafc!important;border-color:#475569!important}
    :root[data-theme='dark'] .content-actions,:root[data-theme='dark'] .content-file-card{background:#111827!important;border-color:#475569!important}

    :root[data-theme='dark'] .toast{background:#111827!important;border-color:#475569!important;color:#f8fafc!important;box-shadow:0 18px 46px rgba(0,0,0,.42)}
    :root[data-theme='dark'] .toast.ok{background:#052e24!important;border-color:#10b981!important;color:#d1fae5!important}
    :root[data-theme='dark'] .toast.warn{background:#451a03!important;border-color:#f59e0b!important;color:#ffedd5!important}
    :root[data-theme='dark'] .toast.error{background:#450a0a!important;border-color:#ef4444!important;color:#fee2e2!important}

    @media(max-width:1300px){.dashboard-status-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-main-grid{grid-template-columns:1fr}.traffic-svg{height:460px}.dashboard-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-feature{grid-template-columns:1fr}.service-grid{grid-template-columns:1fr}}
    @media(max-width:900px){.layout{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);display:flex;gap:14px;overflow:auto;padding:10px 14px}.side-brand{margin:0;min-width:220px;border-radius:18px;min-height:94px}.side-brand-logo{max-width:188px}.sidebar nav{display:flex;gap:8px}.nav-group{display:none}.nav-link{white-space:nowrap;min-width:max-content}.topbar{padding:12px 14px}.search-wrap{max-width:none}.page{padding:16px}.dashboard-grid{grid-template-columns:1fr}.top-actions .top-pill:nth-child(2){display:none}}
    @media(max-width:900px){.content-grid-four,.content-grid-three{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.dashboard-status-grid,.traffic-pair{grid-template-columns:1fr}.traffic-svg{height:380px}.chart-scale-note{align-items:flex-start;flex-direction:column}.access-head{display:none}.access-row{grid-template-columns:1fr;gap:4px}.topbar{flex-wrap:wrap}.search-wrap{order:3;flex:1 0 100%}.page-head{display:block}.mini-grid{grid-template-columns:1fr}.grid,.content-grid-four,.content-grid-three,.grid-compact,.dashboard-grid{grid-template-columns:1fr}table{display:block;overflow-x:auto;white-space:nowrap}.desktop-only{display:none!important}.mobile-only{display:grid}}
    """
    current_admin_user = _get_admin_web_current_user()
    current_admin_name = str((current_admin_user or {}).get("username") or "")
    current_admin_role = _normalize_admin_role((current_admin_user or {}).get("role") or "user") if current_admin_user else ""
    current_admin_label = _admin_role_label(current_admin_role) if current_admin_user else "Não autenticado"
    current_admin_avatar = _admin_initials(current_admin_name) if current_admin_user else "?"
    system_stats_link = ""
    if current_admin_user:
        system_stats_link = "<a class='nav-link' href='/system/stats' data-route='/system/stats'><span class='nav-icon'>▧</span><span>Estatísticas</span></a>"
    system_manage_link = ""
    if current_admin_user and current_admin_role == "admin":
        system_manage_link = "<a class='nav-link' href='/system/users' data-route='/system/users'><span class='nav-icon'>♙</span><span>Gerência</span></a>"
    logout_html = ""
    if current_admin_user:
        logout_html = (
            "<form class='logout-form' method='post' action='/logout'>"
            f"<input type='hidden' name='_csrf' value='{html.escape(_ADMIN_CSRF_TOKEN, quote=True)}'>"
            "<button class='button secondary small-button' type='submit'>Sair</button>"
            "</form>"
        )
    user_chip_html = (
        f"<span class='user-chip'><span class='avatar'>{html.escape(current_admin_avatar)}</span>"
        f"<span><small>{html.escape(current_admin_label)}</small><strong>{html.escape(current_admin_name or 'Login')}</strong></span></span>"
    )
    nav = f"""
      <aside class='sidebar'>
        <a class='side-brand' href='/' aria-label='Servidor Meganet - Telebradesco'><img class='side-brand-logo' src='/logo_side_brand.png' alt='Servidor Meganet - Telebradesco'></a>
        <nav>
          <a class='nav-link' href='/' data-route='/'><span class='nav-icon'>⌂</span><span>Dashboard</span></a>
          <div class='nav-group'>MegaNet</div>
          <a class='nav-link' href='/meganet' data-route='/meganet'><span class='nav-icon'>◎</span><span>Resumo e sessões</span></a>
          <a class='nav-link' href='/content' data-route='/content'><span class='nav-icon'>▣</span><span>Conteúdo</span></a>
          <a class='nav-link' href='/mailboxes' data-route='/mailboxes,/mailbox,/mailbox/message'><span class='nav-icon'>✉</span><span>Correio</span></a>
          <a class='nav-link' href='/users' data-route='/users,/user'><span class='nav-icon'>👤</span><span>Usuários</span></a>
          <a class='nav-link' href='/access-code' data-route='/access-code'><span class='nav-icon'>#</span><span>Gerador de código</span></a>
          <div class='nav-group'>Telebradesco</div>
          <a class='nav-link' href='/telebradesco' data-route='/telebradesco'><span class='nav-icon'>⌘</span><span>Contas e conteúdo</span></a>
          <div class='nav-group'>Sistema</div>
          <a class='nav-link' href='/settings' data-route='/settings'><span class='nav-icon'>⚙</span><span>Configurações</span></a>
          {system_stats_link}
          {system_manage_link}
        </nav>
      </aside>"""
    def _field_guidance(page_title: str) -> str:
        normalized = page_title.strip().casefold()
        if normalized != "conteúdo mega net":
            return ""
        return f"""
      <section class='field-guide'>
        <h2>Preenchimento dos conteúdos</h2>
        <p>Os textos digitados aqui são exibidos nos painéis do MegaNet. Use quebras de linha para separar as linhas que aparecerão na tela.</p>
        <ul>
          <li>Máximo seguro de <b>{MAX_MESSAGE_LINE_LEN} caracteres por linha</b>. Linhas com 36 caracteres exatos provocam avanço duplo na ROM.</li>
          <li>Recomendado: <b>{MAX_CONTENT_RECORD_LINES} linhas</b> por painel; máximo validado: <b>{MAX_CONTENT_RECORD_TOTAL_LINES} linhas</b>.</li>
          <li>O conjunto de cada grupo protocolar deve permanecer abaixo de <b>{MAX_CONTENT_GROUP_SAFE_BYTES:,} bytes</b>.</li>
          <li>Linhas vazias são ignoradas ao salvar.</li>
          <li>No Painel de conexão podem ser usados <code>{{usuario}}</code>, <code>{{email}}</code>, <code>{{mail_count}}</code> e <code>{{mail_status}}</code>.</li>
        </ul>
      </section>"""

    footer = """
      <div class='footer-links'>
        <a href='/'>Dashboard</a>
        <a href='/meganet'>MegaNet</a>
        <a href='/content'>Conteúdo</a>
        <a href='/mailboxes'>Correio</a>
        <a href='/users'>Usuários</a>
        <a href='/telebradesco'>Telebradesco</a>
        <a href='/settings'>Configurações</a>
        <a href='https://github.com/fabriciotm/Servidor-Meganet-Telebradesco' target='_blank' rel='noopener noreferrer'>GitHub</a>
      </div>"""
    script = r"""<script>
    (function(){
      const p=location.pathname||'/';
      document.querySelectorAll('[data-route]').forEach(function(a){
        const rs=(a.dataset.route||'').split(',');
        const on=rs.some(function(r){return r==='/'?p==='/' : p===r||p.startsWith(r+'/');});
        a.classList.toggle('active',on);
      });
    })();
    (function(){
      const key='smt-theme';
      const root=document.documentElement;
      const toggle=document.getElementById('themeToggle');
      function apply(mode){
        const dark=mode==='dark';
        root.setAttribute('data-theme',dark?'dark':'light');
        if(toggle)toggle.checked=dark;
      }
      let saved='light';
      try{saved=localStorage.getItem(key)||'light';}catch(e){}
      apply(saved);
      if(toggle){
        toggle.addEventListener('change',function(){
          const mode=toggle.checked?'dark':'light';
          apply(mode);
          try{localStorage.setItem(key,mode);}catch(e){}
        });
      }
    })();
    (function(){
      const statusParams=['saved','reloaded','deleted','json_saved','sent','created','updated','error','truncated','no_access'];
      function ensureContainer(){
        let box=document.querySelector('.toast-container');
        if(!box){
          box=document.createElement('div');
          box.className='toast-container';
          box.setAttribute('aria-live','polite');
          box.setAttribute('aria-atomic','true');
          document.body.appendChild(box);
        }
        return box;
      }
      function kindFrom(node){
        if(!node) return 'ok';
        if(node.classList && node.classList.contains('warn')) return 'warn';
        if(node.classList && node.classList.contains('error')) return 'error';
        if(node.querySelector && node.querySelector('.warn')) return 'warn';
        if(node.querySelector && node.querySelector('.error,.login-error')) return 'error';
        return 'ok';
      }
      function showToast(html,kind){
        const text=(html||'').replace(/<[^>]*>/g,'').trim();
        if(!text) return;
        const box=ensureContainer();
        const toast=document.createElement('div');
        toast.className='toast '+(kind||'ok');
        toast.setAttribute('role',kind==='error'||kind==='warn'?'alert':'status');
        toast.innerHTML='<button type="button" class="toast-close" aria-label="Fechar aviso">×</button><div class="toast-body">'+html+'</div>';
        box.appendChild(toast);
        const close=function(){
          toast.classList.add('hide');
          window.setTimeout(function(){toast.remove();},260);
        };
        const btn=toast.querySelector('.toast-close');
        if(btn) btn.addEventListener('click',close);
        window.setTimeout(function(){toast.classList.add('show');},20);
        window.setTimeout(close,5600);
      }
      function isStatusOnlyCard(card){
        if(!card || card.dataset.toastHandled==='1') return false;
        if(!card.querySelector('p.ok,p.warn,.flash')) return false;
        if(card.querySelector('form,table,textarea,input,select,details,.metric,.kpi-card,.content-item')) return false;
        const children=Array.prototype.slice.call(card.children).filter(function(el){return (el.textContent||'').trim() || el.children.length;});
        if(!children.length) return false;
        return children.every(function(el){
          return el.matches('p.ok,p.warn,p.muted,.flash') || (el.matches('div') && el.querySelector('p.ok,p.warn,.flash'));
        });
      }
      document.querySelectorAll('.flash').forEach(function(el){
        if(el.closest('.toast-container')) return;
        showToast(el.innerHTML,kindFrom(el));
        el.remove();
      });
      document.querySelectorAll('.page > .card').forEach(function(card){
        if(!isStatusOnlyCard(card)) return;
        card.dataset.toastHandled='1';
        showToast(card.innerHTML,kindFrom(card));
        card.remove();
      });
      document.querySelectorAll('.page > p.ok,.page > p.warn').forEach(function(el){
        showToast(el.innerHTML,kindFrom(el));
        el.remove();
      });
      try{
        const url=new URL(window.location.href);
        let changed=false;
        statusParams.forEach(function(name){
          if(url.searchParams.has(name)){
            url.searchParams.delete(name);
            changed=true;
          }
        });
        if(changed){
          const next=url.pathname+(url.searchParams.toString()?('?'+url.searchParams.toString()):'')+url.hash;
          window.history.replaceState(null,document.title,next);
        }
      }catch(e){}
    })();
    </script>"""
    dashboard = title.strip().lower() in {"painel", "dashboard"}
    dashboard_class = " dashboard-page" if dashboard else ""
    supplemental = "" if dashboard else _field_guidance(title)
    footer_html = footer
    doc=f"""<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)} — Servidor Meganet - Telebradesco</title><link rel='icon' href='/favicon.ico'><style>{css}</style></head><body><div class='layout'>{nav}<div class='content'><header class='topbar'><div class='top-actions' style='margin-left:auto'><label class='theme-switch' title='Alternar modo escuro'><input id='themeToggle' type='checkbox' aria-label='Modo escuro'><span></span><em>Escuro</em></label><span class='top-pill'><span class='status-dot'></span>Servidor online</span>{user_chip_html}{logout_html}</div></header><main class='page{dashboard_class}'><div class='page-head'><div><h1>{html.escape(title)}</h1><p>{'Visão geral operacional dos serviços' if dashboard else 'Administração integrada dos serviços MegaNet e Telebradesco'}</p></div></div>{supplemental}{body}{footer_html}</main></div></div>{script}</body></html>"""
    return doc.encode('utf-8')
