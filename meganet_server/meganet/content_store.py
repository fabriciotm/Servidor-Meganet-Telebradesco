"""Persistent MegaNet editorial content configuration and live reload helpers."""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
from typing import Optional

from ..common import utc_now_iso
from ..storage import atomic_write_json
from .content import (
    CONNECTION_PANEL_PRESETS, DEFAULT_CONTENT_DIR, DEFAULT_CONTENT_FILE, DEFAULT_MESSAGE,
    DEFAULT_TECTOY_NEWS, merge_content_records, normalize_message_lines,
)

CONTENT_FILE_FIELDS = {"connection_panel_lines", "message_lines", "tectoy_news_lines", "content_records"}

def content_file_path(content_dir: object) -> pathlib.Path:
    base = pathlib.Path(content_dir or DEFAULT_CONTENT_DIR)
    if base.suffix.lower() == ".json":
        return base
    return base / DEFAULT_CONTENT_FILE

def content_directory_path(content_dir: object) -> pathlib.Path:
    base = pathlib.Path(content_dir or DEFAULT_CONTENT_DIR)
    if base.suffix.lower() == ".json":
        return base.parent
    return base

def sanitize_content_json_filename(filename: object) -> str:
    text = str(filename or "").strip().replace("\\", "/")
    name = pathlib.PurePath(text).name
    if not name or name in {".", ".."}:
        return ""
    if not name.lower().endswith(".json"):
        return ""
    return name

def list_content_json_files(content_dir: object) -> list[str]:
    base = content_directory_path(content_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    files: list[str] = []
    try:
        for item in base.iterdir():
            if item.is_file() and item.suffix.lower() == ".json":
                files.append(item.name)
    except OSError as exc:
        logging.warning("CONTENT_FILE_LIST_FAILED dir=%s error=%s", base, exc)
    active_name = content_file_path(content_dir).name
    if active_name and active_name.lower().endswith(".json") and active_name not in files:
        files.append(active_name)
    if DEFAULT_CONTENT_FILE not in files:
        files.insert(0, DEFAULT_CONTENT_FILE)
    return sorted(set(files), key=lambda x: (x.lower() != DEFAULT_CONTENT_FILE.lower(), x.lower()))

def select_content_json_file(args: argparse.Namespace, filename: object) -> Optional[pathlib.Path]:
    name = sanitize_content_json_filename(filename)
    if not name:
        return None
    base = content_directory_path(getattr(args, "content_dir", DEFAULT_CONTENT_DIR))
    selected = (base / name).resolve()
    try:
        base_resolved = base.resolve()
        selected.relative_to(base_resolved)
    except (OSError, ValueError):
        return None
    setattr(args, "content_dir", selected)
    return selected

def default_content_config() -> dict:
    now = utc_now_iso()
    return {
        "schema": "meganet_content_v1",
        "created_at": now,
        "updated_at": now,
        "connection_panel_lines": CONNECTION_PANEL_PRESETS["unified"],
        "message_lines": DEFAULT_MESSAGE,
        "tectoy_news_lines": DEFAULT_TECTOY_NEWS,
        "content_records": merge_content_records(None),
    }

def _extract_legacy_content(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}
    out: dict[str, object] = {}
    for key in CONTENT_FILE_FIELDS:
        if key in config:
            out[key] = config[key]
    return out

def normalize_content_config(raw: object, *, legacy_config: Optional[dict] = None) -> dict:
    data = default_content_config()
    legacy = _extract_legacy_content(legacy_config or {})
    if legacy:
        data.update(legacy)
    if isinstance(raw, dict):
        data.update({key: raw[key] for key in CONTENT_FILE_FIELDS if key in raw})
        if isinstance(raw.get("created_at"), str):
            data["created_at"] = raw["created_at"]
    data["connection_panel_lines"] = normalize_message_lines(data.get("connection_panel_lines", []))
    data["message_lines"] = normalize_message_lines(data.get("message_lines", []))
    data["tectoy_news_lines"] = normalize_message_lines(data.get("tectoy_news_lines", []))
    data["content_records"] = merge_content_records(data.get("content_records"))
    data["schema"] = "meganet_content_v1"
    data["updated_at"] = utc_now_iso()
    return data

def read_content_file(content_dir: object, *, legacy_config: Optional[dict] = None, create_if_missing: bool = True) -> dict:
    path = content_file_path(content_dir)
    raw: object = None
    existed = path.exists()
    if existed:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logging.warning("CONTENT_READ_FAILED path=%s error=%s; using defaults/legacy content", path, exc)
            raw = None
    data = normalize_content_config(raw, legacy_config=legacy_config if not existed else None)
    # Only create a file that is genuinely absent. If an existing file cannot
    # be decoded/read, keep it intact for diagnosis or manual recovery and use
    # the normalized defaults in memory for this process.
    if create_if_missing and not existed:
        write_content_file(content_dir, data)
    return data

def current_content_from_args(args_or_content: object) -> dict:
    if isinstance(args_or_content, argparse.Namespace):
        raw = {key: getattr(args_or_content, key, None) for key in CONTENT_FILE_FIELDS}
    elif isinstance(args_or_content, dict):
        raw = args_or_content
    else:
        raw = {}
    return normalize_content_config(raw)

def write_content_file(content_dir: object, args_or_content: object) -> pathlib.Path:
    path = content_file_path(content_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = current_content_from_args(args_or_content)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                data["created_at"] = existing["created_at"]
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    atomic_write_json(path, data, ensure_ascii=False, indent=2)
    return path

def apply_content_to_args(args: argparse.Namespace, content: dict) -> None:
    args.connection_panel_lines = normalize_message_lines(content.get("connection_panel_lines", []))
    args.message_lines = normalize_message_lines(content.get("message_lines", []))
    args.tectoy_news_lines = normalize_message_lines(content.get("tectoy_news_lines", []))
    args.content_records = merge_content_records(content.get("content_records"))

def _copy_meganet_content_runtime_fields(source: argparse.Namespace, target: argparse.Namespace) -> None:
    """Copy live MegaNet content fields from the admin/root config to a service clone.

    TCP/serial listeners are created from service-specific Namespace clones.  Those
    clones must receive the refreshed content explicitly; otherwise a web save or
    reload updates only the admin Namespace and the cartridge keeps downloading
    the content captured when the script started.
    """
    for key in (
        "content_dir",
        "connection_panel_lines",
        "message_lines",
        "tectoy_news_lines",
        "content_records",
        "content_profile",
        "routing_mode",
        "news_route_map",
        "connection_panel_mail_count",
        "meganet_packet_ack_mode",
        "meganet_packet_ack_timeout",
        "meganet_packet_max_retries",
        "meganet_bootstrap_noise_tolerance",
        "meganet_bootstrap_noise_window",
        # Runtime identity/mail settings must follow the root/admin Namespace as
        # well.  Otherwise service clones keep startup paths/domains until restart.
        "cadastro_db",
        "mail_store_dir",
        "mail_inbox",
        "mail_domain",
        "admin_email",
        "welcome_email_enabled",
        "welcome_email_from",
        "welcome_email_subject",
        "welcome_email_body",
        "password_change_store_dir",
        "password_change_apply",
    ):
        if hasattr(source, key):
            value = getattr(source, key)
            if key == "content_records":
                value = merge_content_records(value)
            elif key in {"connection_panel_lines", "message_lines", "tectoy_news_lines"}:
                value = normalize_message_lines(value)
            elif key == "news_route_map" and isinstance(value, dict):
                value = {str(k): str(v) for k, v in value.items()}
            setattr(target, key, value)

def _propagate_meganet_content_to_service_args(root_args: argparse.Namespace) -> None:
    refs = getattr(root_args, "_service_arg_refs", None)
    if not isinstance(refs, dict):
        return
    for service, service_args in list(refs.items()):
        if str(service).lower() == "meganet" and isinstance(service_args, argparse.Namespace):
            _copy_meganet_content_runtime_fields(root_args, service_args)

def reload_active_meganet_content(root_args: argparse.Namespace, *, legacy_config: Optional[dict] = None, create_if_missing: bool = True) -> pathlib.Path:
    content_cfg = read_content_file(
        getattr(root_args, "content_dir", DEFAULT_CONTENT_DIR),
        legacy_config=legacy_config,
        create_if_missing=create_if_missing,
    )
    apply_content_to_args(root_args, content_cfg)
    _propagate_meganet_content_to_service_args(root_args)
    return content_file_path(getattr(root_args, "content_dir", DEFAULT_CONTENT_DIR))

def _prepare_meganet_session_args(service_args: argparse.Namespace) -> argparse.Namespace:
    root_args = getattr(service_args, "_root_args", service_args)
    if not isinstance(root_args, argparse.Namespace):
        root_args = service_args
    try:
        reload_active_meganet_content(root_args, create_if_missing=True)
        if root_args is not service_args:
            _copy_meganet_content_runtime_fields(root_args, service_args)
    except Exception as exc:
        logging.warning(
            "MEGANET_CONTENT_RUNTIME_REFRESH_FAILED content=%s error=%s",
            content_file_path(getattr(root_args, "content_dir", DEFAULT_CONTENT_DIR)),
            exc,
        )
    return service_args

