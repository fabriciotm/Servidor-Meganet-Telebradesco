#!/usr/bin/env python3
"""Small shared helpers used by administrative controllers."""

from __future__ import annotations

import argparse
import html

def _request_close_meganet_sessions(args: argparse.Namespace, reason: str) -> int:
    runtime = getattr(args, "_service_runtime", None)
    if not runtime:
        return 0
    if hasattr(runtime, "request_close_service"):
        return int(runtime.request_close_service("meganet", reason) or 0)
    return int(runtime.request_close_all(reason) or 0)

def _admin_opt(name: str, choices: list[str], cfg: dict) -> str:
    cur = str(cfg.get(name, ""))
    return "".join(
        f'<option value="{html.escape(choice)}" {"selected" if choice == cur else ""}>{html.escape(choice)}</option>'
        for choice in choices
    )

def _admin_yes(value: object) -> str:
    return "checked" if bool(value) else ""

def _safe_field_value(value: object) -> str:
    text = "" if value is None else str(value)
    return html.escape(text, quote=True)
