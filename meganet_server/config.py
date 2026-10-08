"""Canonical configuration model and persistence for MegaNet / Telebradesco.

The typed dataclasses are the runtime-facing view.  Legacy ``argparse.Namespace``
remains supported for compatibility, while JSON persistence,
coercion, path resolution and layout migration are centralized here.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import logging
import pathlib
import shutil
from typing import Any, Optional

from .meganet.accounts import DEFAULT_CADASTRO_DB, DEFAULT_SESSION_OUTPUT_DIR
from .meganet.content import (
    DEFAULT_CONTENT_DIR,
    DEFAULT_CONTENT_FILE,
    MAX_MESSAGE_LINE_LEN,
    merge_content_records,
    normalize_message_lines,
)
from .meganet.content_store import CONTENT_FILE_FIELDS
from .meganet.mail import DEFAULT_MAIL_STORE_DIR, DEFAULT_WELCOME_EMAIL_BODY, DEFAULT_WELCOME_EMAIL_SUBJECT
from .meganet.session import DEFAULT_PASSWORD_CHANGE_STORE_DIR
from .telebradesco.accounts import DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE, DEFAULT_TELEBRADESCO_ACCOUNTS_FILE
from .storage import atomic_write_json, read_json_dict


def normalize_service_transport(value: object) -> str:
    """Normalize a service transport to the supported endpoint modes."""
    mode = str(value or "off").strip().lower()
    return mode if mode in {"off", "tcp", "serial", "both"} else "off"


def _choice(value: object, allowed: set[str], default: str) -> str:
    text = str(value or default).strip().lower()
    return text if text in allowed else default


def normalize_bind_host(value: object, *, default: str = "127.0.0.1") -> str:
    """Normalize bind aliases while preserving explicit host names/addresses."""
    text = str(value or "").strip()
    if not text:
        return default
    lowered = text.casefold()
    if lowered in {"0", "*", "all", "any", "todas", "todas as interfaces", "0.0.0.0"}:
        return "0.0.0.0"
    return text


def _host(value: object, default: str = "127.0.0.1") -> str:
    return normalize_bind_host(value, default=default)


def _port(value: object, default: int, *, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(65535, number))


def _float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _path(value: object, default: str) -> pathlib.Path:
    if isinstance(value, pathlib.Path):
        return value
    return pathlib.Path(str(value or default))


@dataclass(frozen=True, slots=True)
class ServiceEndpointConfig:
    transport: str
    host: str
    port: int
    serial_port: str
    serial_baud: int

    @classmethod
    def from_namespace(cls, ns: Any, prefix: str, *, default_port: int, default_serial: str) -> "ServiceEndpointConfig":
        return cls(
            transport=normalize_service_transport(getattr(ns, f"{prefix}_transport", "off")),
            host=_host(getattr(ns, f"{prefix}_host", "127.0.0.1")),
            port=_port(getattr(ns, f"{prefix}_port", default_port), default_port),
            serial_port=str(getattr(ns, f"{prefix}_serial_port", default_serial) or default_serial),
            serial_baud=_int(getattr(ns, f"{prefix}_serial_baud", 115200), 115200, 300, 4_000_000),
        )


@dataclass(frozen=True, slots=True)
class MegaNetConfig:
    endpoint: ServiceEndpointConfig
    auth_mode: str
    password_case: str
    packet_ack_mode: str
    packet_ack_timeout: float
    packet_max_retries: int
    bootstrap_noise_tolerance: int
    bootstrap_noise_window: float
    auto_ready_timeout: float
    cadastro_db: pathlib.Path
    content_dir: pathlib.Path
    mail_store_dir: pathlib.Path
    password_change_store_dir: pathlib.Path
    output_dir: pathlib.Path

    @classmethod
    def from_namespace(cls, ns: Any) -> "MegaNetConfig":
        ack_mode = str(getattr(ns, "meganet_packet_ack_mode", "all") or "all").strip().lower()
        if ack_mode not in {"off", "transfers", "all"}:
            ack_mode = "all"
        return cls(
            endpoint=ServiceEndpointConfig.from_namespace(ns, "meganet", default_port=19777, default_serial="COM12"),
            auth_mode=str(getattr(ns, "meganet_auth_mode", "require-known") or "require-known"),
            password_case=str(getattr(ns, "meganet_password_case", "case-insensitive") or "case-insensitive"),
            packet_ack_mode=ack_mode,
            packet_ack_timeout=_float(getattr(ns, "meganet_packet_ack_timeout", 3.0), 3.0, 0.20, 30.0),
            packet_max_retries=_int(getattr(ns, "meganet_packet_max_retries", 5), 5, 1, 10),
            bootstrap_noise_tolerance=_int(getattr(ns, "meganet_bootstrap_noise_tolerance", 2), 2, 0, 32),
            bootstrap_noise_window=_float(getattr(ns, "meganet_bootstrap_noise_window", 2.0), 2.0, 0.10, 30.0),
            auto_ready_timeout=_float(getattr(ns, "meganet_auto_ready_timeout", 12.0), 12.0, 0.50, 30.0),
            cadastro_db=_path(getattr(ns, "cadastro_db", None), "meganet/cadastros.json"),
            content_dir=_path(getattr(ns, "content_dir", None), "meganet/content"),
            mail_store_dir=_path(getattr(ns, "mail_store_dir", None), "meganet/mail"),
            password_change_store_dir=_path(getattr(ns, "password_change_store_dir", None), "meganet/password"),
            output_dir=_path(getattr(ns, "output_dir", None), "meganet/sessions"),
        )


@dataclass(frozen=True, slots=True)
class TelebradescoAdapterConfig:
    enabled: bool
    host: str
    port: int
    timeout: float
    log_file: pathlib.Path

    @classmethod
    def from_namespace(cls, ns: Any) -> "TelebradescoAdapterConfig":
        return cls(
            enabled=bool(getattr(ns, "telebradesco_adapter_enabled", True)),
            host=_host(getattr(ns, "telebradesco_adapter_host", "127.0.0.1")),
            port=_port(getattr(ns, "telebradesco_adapter_port", 19780), 19780),
            timeout=_float(getattr(ns, "telebradesco_adapter_timeout", 5.0), 5.0, 0.20, 30.0),
            log_file=_path(getattr(ns, "telebradesco_adapter_log_file", None), "logs/telebradesco_adapter.jsonl"),
        )


@dataclass(frozen=True, slots=True)
class TelebradescoConfig:
    endpoint: ServiceEndpointConfig
    accounts_file: pathlib.Path
    content_file: pathlib.Path
    block_mode: str
    error_hold_seconds: float
    adapter: TelebradescoAdapterConfig

    @classmethod
    def from_namespace(cls, ns: Any) -> "TelebradescoConfig":
        block_mode = str(getattr(ns, "telebradesco_block_mode", "native-020") or "native-020").strip().lower()
        if block_mode not in {"native-020", "deny-login"}:
            block_mode = "native-020"
        return cls(
            endpoint=ServiceEndpointConfig.from_namespace(ns, "telebradesco", default_port=19779, default_serial="COM10"),
            accounts_file=_path(getattr(ns, "telebradesco_accounts_file", None), "telebradesco/accounts.json"),
            content_file=_path(getattr(ns, "telebradesco_content_file", None), "telebradesco/accounts_content.json"),
            block_mode=block_mode,
            error_hold_seconds=_float(getattr(ns, "telebradesco_error_hold_seconds", 2.0), 2.0, 0.0, 30.0),
            adapter=TelebradescoAdapterConfig.from_namespace(ns),
        )


@dataclass(frozen=True, slots=True)
class AdminConfig:
    enabled: bool
    host: str
    port: int

    @classmethod
    def from_namespace(cls, ns: Any) -> "AdminConfig":
        return cls(
            enabled=bool(getattr(ns, "admin_web", True)),
            host=_host(getattr(ns, "admin_host", "127.0.0.1")),
            port=_port(getattr(ns, "admin_port", 19778), 19778, minimum=0),
        )


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    default_service: str
    detect_timeout: float

    @classmethod
    def from_namespace(cls, ns: Any) -> "ListenerConfig":
        default_service = str(getattr(ns, "unified_tcp_default_service", "meganet") or "meganet").strip().lower()
        if default_service not in {"meganet", "telebradesco"}:
            default_service = "meganet"
        return cls(
            default_service=default_service,
            detect_timeout=_float(getattr(ns, "unified_tcp_detect_timeout", 3.0), 3.0, 0.20, 30.0),
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    meganet: MegaNetConfig
    telebradesco: TelebradescoConfig
    admin: AdminConfig
    listener: ListenerConfig
    log_level: str
    config_file: pathlib.Path | None

    @classmethod
    def from_namespace(cls, ns: Any) -> "AppConfig":
        config_value = getattr(ns, "config", None)
        return cls(
            meganet=MegaNetConfig.from_namespace(ns),
            telebradesco=TelebradescoConfig.from_namespace(ns),
            admin=AdminConfig.from_namespace(ns),
            listener=ListenerConfig.from_namespace(ns),
            log_level=str(getattr(ns, "log_level", "INFO") or "INFO").upper(),
            config_file=pathlib.Path(config_value) if config_value else None,
        )

    def endpoint(self, service: str) -> ServiceEndpointConfig:
        return self.telebradesco.endpoint if str(service).lower() == "telebradesco" else self.meganet.endpoint


def refresh_app_config(ns: Any) -> AppConfig:
    """Rebuild and attach the typed configuration after legacy Namespace edits."""
    config = AppConfig.from_namespace(ns)
    setattr(ns, "app_config", config)
    return config


# ---------------------------------------------------------------------------
# Legacy-compatible operational configuration persistence
# ---------------------------------------------------------------------------

CONFIG_MANAGED_FIELDS = {
    "active_service", "telebradesco_content_file", "telebradesco_accounts_file", "telebradesco_block_mode", "telebradesco_error_hold_seconds",
    "telebradesco_adapter_enabled", "telebradesco_adapter_host", "telebradesco_adapter_port", "telebradesco_adapter_timeout", "telebradesco_adapter_log_file",
    "host", "port", "server_transport", "serial_port", "serial_baud",
    "meganet_transport", "meganet_host", "meganet_port", "meganet_serial_port", "meganet_serial_baud",
    "telebradesco_transport", "telebradesco_host", "telebradesco_port", "telebradesco_serial_port", "telebradesco_serial_baud",
    "online_start", "profile_idle", "post_dle_delay",
    "enable_session_logs", "output_dir", "content_dir",
    "cadastro_db", "meganet_auth_mode", "meganet_password_case",
    "registration_duplicate_action", "codigo_acesso_duplicate_action",
    "transaction_close", "transaction_close_delay", "post_id_action",
    "post_id_message_delay", "outbound_line_max_len", "outbound_packet_delay",
    "meganet_packet_ack_mode", "meganet_packet_ack_timeout", "meganet_packet_max_retries",
    "meganet_bootstrap_noise_tolerance", "meganet_bootstrap_noise_window",
    "unknown_token_action", "raw_hayes_in_data_action", "raw_hayes_close_delay",
    "routing_mode", "content_profile", "connection_panel_preset",
    "connection_panel_mail_count", "mail_store_dir", "mail_inbox", "mail_domain",
    "admin_email", "welcome_email_enabled", "welcome_email_from", "welcome_email_subject", "welcome_email_body",
    "password_change_store_dir", "password_change_apply",
    "log_level", "news_route_map",
    "admin_web", "admin_host", "admin_port",
    "unified_tcp_default_service", "unified_tcp_detect_timeout",
    "meganet_auto_ready_timeout",
}

PATH_FIELDS = {"output_dir", "content_dir", "telebradesco_content_file", "telebradesco_accounts_file", "telebradesco_adapter_log_file", "cadastro_db", "mail_store_dir", "mail_inbox", "password_change_store_dir"}
BOOL_FIELDS = {"online_start", "enable_session_logs", "password_change_apply", "admin_web", "welcome_email_enabled", "telebradesco_adapter_enabled"}
INT_FIELDS = {"port", "outbound_line_max_len", "admin_port", "serial_baud",
              "meganet_port", "meganet_serial_baud",
              "telebradesco_port", "telebradesco_serial_baud", "telebradesco_adapter_port",
              "meganet_packet_max_retries", "meganet_bootstrap_noise_tolerance"}
FLOAT_FIELDS = {"profile_idle", "post_dle_delay", "transaction_close_delay", "post_id_message_delay", "outbound_packet_delay", "raw_hayes_close_delay", "telebradesco_error_hold_seconds", "telebradesco_adapter_timeout", "unified_tcp_detect_timeout", "meganet_auto_ready_timeout", "meganet_packet_ack_timeout", "meganet_bootstrap_noise_window"}


def _bind_display_host(value: object) -> str:
    host = normalize_bind_host(value)
    return "0.0.0.0" if host == "0.0.0.0" else host


def default_operational_config() -> dict:
    return {
        "active_service": "meganet",
        "telebradesco_content_file": str(DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE),
        "telebradesco_accounts_file": str(DEFAULT_TELEBRADESCO_ACCOUNTS_FILE),
        "telebradesco_block_mode": "native-020",
        "telebradesco_error_hold_seconds": 2.0,
        "telebradesco_adapter_enabled": True,
        "telebradesco_adapter_host": "127.0.0.1",
        "telebradesco_adapter_port": 19780,
        "telebradesco_adapter_timeout": 5.0,
        "telebradesco_adapter_log_file": "logs/telebradesco_adapter.jsonl",
        "host": "127.0.0.1",
        "port": 19777,
        "server_transport": "tcp",
        "serial_port": "COM10",
        "serial_baud": 115200,
        "meganet_transport": "tcp",
        "meganet_host": "127.0.0.1",
        "meganet_port": 19777,
        "meganet_serial_port": "COM12",
        "meganet_serial_baud": 115200,
        "telebradesco_transport": "tcp",
        "telebradesco_host": "127.0.0.1",
        "telebradesco_port": 19779,
        "telebradesco_serial_port": "COM10",
        "telebradesco_serial_baud": 115200,
        "online_start": True,
        "profile_idle": 0.25,
        "post_dle_delay": 0.20,
        "enable_session_logs": False,
        "output_dir": str(DEFAULT_SESSION_OUTPUT_DIR),
        "content_dir": str(DEFAULT_CONTENT_DIR),
        "cadastro_db": str(DEFAULT_CADASTRO_DB),
        "meganet_auth_mode": "require-known",
        "meganet_password_case": "case-insensitive",
        "registration_duplicate_action": "reject-message",
        "codigo_acesso_duplicate_action": "reject-message",
        "transaction_close": "off",
        "transaction_close_delay": 0.35,
        "post_id_action": "id-body-hash-end-ready2-wait",
        "post_id_message_delay": 0.15,
        "outbound_line_max_len": MAX_MESSAGE_LINE_LEN,
        "outbound_packet_delay": 0.10,
        "meganet_packet_ack_mode": "all",
        "meganet_packet_ack_timeout": 3.0,
        "meganet_packet_max_retries": 5,
        "meganet_bootstrap_noise_tolerance": 2,
        "meganet_bootstrap_noise_window": 2.0,
        "unknown_token_action": "message-ready",
        "raw_hayes_in_data_action": "ok-close",
        "raw_hayes_close_delay": 0.20,
        "routing_mode": "unified",
        "content_profile": "tectoy-news",
        "connection_panel_preset": "unified",
        "connection_panel_mail_count": "auto",
        "news_route_map": {"1": "tectoy-news", "2": "internet", "3": "revista", "4": "outros"},
        "mail_store_dir": str(DEFAULT_MAIL_STORE_DIR),
        "mail_inbox": None,
        "mail_domain": "meganet.tectoy.com.br",
        "admin_email": "admin@meganet.tectoy.com.br",
        "welcome_email_enabled": True,
        "welcome_email_from": "admin@meganet.tectoy.com.br",
        "welcome_email_subject": DEFAULT_WELCOME_EMAIL_SUBJECT,
        "welcome_email_body": "\n".join(DEFAULT_WELCOME_EMAIL_BODY),
        "password_change_store_dir": str(DEFAULT_PASSWORD_CHANGE_STORE_DIR),
        "password_change_apply": True,
        "log_level": "INFO",
        "admin_web": True,
        "admin_host": "127.0.0.1",
        "admin_port": 19778,
        "unified_tcp_default_service": "meganet",
        "unified_tcp_detect_timeout": 3.0,
        "meganet_auto_ready_timeout": 12.0,
    }


def _explicit_cli_keys(argv: list[str]) -> set[str]:
    out: set[str] = set()
    for token in argv:
        if token.startswith("--"):
            name = token.split("=", 1)[0][2:].replace("-", "_")
            if name.startswith("no_"):
                name = name[3:]
            if name == "session_logs":
                name = "enable_session_logs"
            if name == "welcome_email":
                name = "welcome_email_enabled"
            if name == "telebradesco_adapter":
                name = "telebradesco_adapter_enabled"
            out.add(name)
    return out


OPERATIONAL_CHOICE_FIELDS: dict[str, tuple[set[str], str]] = {
    "meganet_auth_mode": ({"off", "allow-new", "require-known"}, "require-known"),
    "meganet_password_case": ({"exact", "upper", "lower", "case-insensitive"}, "case-insensitive"),
    "registration_duplicate_action": ({"reject-message", "allow-update"}, "reject-message"),
    "codigo_acesso_duplicate_action": ({"reject-message", "allow"}, "reject-message"),
    "transaction_close": ({"after-final-message", "after-bye", "off"}, "off"),
    "unknown_token_action": ({"none", "ready", "message-ready"}, "message-ready"),
    "raw_hayes_in_data_action": ({"ignore", "close", "ok-close"}, "ok-close"),
    "routing_mode": ({"unified", "manual"}, "unified"),
    "content_profile": ({"tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"}, "tectoy-news"),
}


def normalize_endpoint_args(args: argparse.Namespace) -> None:
    """Keep legacy Namespace endpoint fields identical to the typed runtime view."""
    for prefix, default_port, default_serial in (
        ("meganet", 19777, "COM12"),
        ("telebradesco", 19779, "COM10"),
    ):
        endpoint = ServiceEndpointConfig.from_namespace(
            args, prefix, default_port=default_port, default_serial=default_serial
        )
        setattr(args, f"{prefix}_transport", endpoint.transport)
        setattr(args, f"{prefix}_host", endpoint.host)
        setattr(args, f"{prefix}_port", endpoint.port)
        setattr(args, f"{prefix}_serial_port", endpoint.serial_port)
        setattr(args, f"{prefix}_serial_baud", endpoint.serial_baud)


def _coerce_config_value(key: str, value: object) -> object:
    if key in OPERATIONAL_CHOICE_FIELDS:
        allowed, default = OPERATIONAL_CHOICE_FIELDS[key]
        return _choice(value, allowed, default)
    if key in {"host", "meganet_host", "telebradesco_host", "telebradesco_adapter_host", "admin_host"}:
        return normalize_bind_host(value)
    if key in PATH_FIELDS:
        return pathlib.Path(value) if value not in (None, "") else None
    if key in BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "sim", "on"}
    if key in INT_FIELDS:
        # Empty numeric fields mean "use the operational default".  This keeps
        # admin reloads tolerant of old forms/configs and avoids int('').
        if value is None or (isinstance(value, str) and not value.strip()):
            value = default_operational_config().get(key, 0)
        intval = int(value)
        if key == "outbound_line_max_len":
            return min(MAX_MESSAGE_LINE_LEN, max(8, intval))
        if key == "meganet_packet_max_retries":
            return max(1, min(10, intval))
        if key == "meganet_bootstrap_noise_tolerance":
            return max(0, min(32, intval))
        if key == "admin_port":
            return max(0, min(65535, intval))
        return intval
    if key in FLOAT_FIELDS:
        if value is None or (isinstance(value, str) and not value.strip()):
            value = default_operational_config().get(key, 0.0)
        floatval = float(value)
        if key == "meganet_packet_ack_timeout":
            return max(0.20, min(30.0, floatval))
        if key == "meganet_bootstrap_noise_window":
            return max(0.10, min(30.0, floatval))
        if key == "unified_tcp_detect_timeout":
            return max(0.20, min(30.0, floatval))
        return floatval
    if key in {"connection_panel_lines", "message_lines"}:
        if isinstance(value, str):
            return normalize_message_lines(value.splitlines())
        if isinstance(value, list):
            return normalize_message_lines([str(x) for x in value])
        return []
    if key == "content_records":
        return merge_content_records(value)
    if key == "news_route_map":
        defaults = {"1": "tectoy-news", "2": "internet", "3": "revista", "4": "outros"}
        allowed_profiles = OPERATIONAL_CHOICE_FIELDS["content_profile"][0]
        result = defaults.copy()
        if isinstance(value, dict):
            for raw_group, raw_profile in value.items():
                group = str(raw_group).strip()
                profile = str(raw_profile or "").strip().lower()
                if group and profile in allowed_profiles:
                    result[group] = profile
        return result
    if key == "unified_tcp_default_service":
        text = str(value or "meganet").strip().lower()
        return text if text in {"meganet", "telebradesco"} else "meganet"
    if key == "meganet_packet_ack_mode":
        text = str(value or "all").strip().lower()
        return text if text in {"off", "transfers", "all"} else "all"
    if key == "log_level":
        text = str(value or "INFO").strip().upper()
        if text not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"invalid log level: {value!r}")
        return text
    return value


def _json_ready(value: object) -> object:
    if isinstance(value, pathlib.Path):
        return str(value)
    return value




def _resolve_runtime_paths(args: argparse.Namespace, base_dir: pathlib.Path) -> None:
    base = pathlib.Path(base_dir).expanduser().resolve()
    args._config_base_dir = base
    for key in PATH_FIELDS:
        value = getattr(args, key, None)
        if value in (None, ""):
            continue
        path = pathlib.Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
        setattr(args, key, path.resolve())


def _config_path_value(args: argparse.Namespace, value: object) -> object:
    if not isinstance(value, pathlib.Path):
        return value
    base = pathlib.Path(getattr(args, "_config_base_dir", pathlib.Path.cwd())).resolve()
    path = value.expanduser().resolve()
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


LEGACY_DEFAULT_CONFIG_PATHS: dict[str, tuple[str, pathlib.Path]] = {
    "content_dir": ("meganet_content", DEFAULT_CONTENT_DIR),
    "cadastro_db": ("meganet_cadastros.json", DEFAULT_CADASTRO_DB),
    "mail_store_dir": ("meganet_mail", DEFAULT_MAIL_STORE_DIR),
    "mail_inbox": ("meganet_mail/inbox.json", DEFAULT_MAIL_STORE_DIR / "inbox.json"),
    "password_change_store_dir": ("meganet_password", DEFAULT_PASSWORD_CHANGE_STORE_DIR),
    "output_dir": ("meganet_sessions", DEFAULT_SESSION_OUTPUT_DIR),
    "telebradesco_accounts_file": ("meganet_content/telebradesco_accounts.json", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE),
    "telebradesco_content_file": ("meganet_content/telebradesco.json", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE),
}

LEGACY_LAYOUT_ARTIFACTS: tuple[tuple[str, str, pathlib.Path], ...] = (
    ("content_dir", "meganet_content/content.json", DEFAULT_CONTENT_DIR / DEFAULT_CONTENT_FILE),
    ("cadastro_db", "meganet_cadastros.json", DEFAULT_CADASTRO_DB),
    ("mail_store_dir", "meganet_mail", DEFAULT_MAIL_STORE_DIR),
    ("password_change_store_dir", "meganet_password", DEFAULT_PASSWORD_CHANGE_STORE_DIR),
    ("output_dir", "meganet_sessions", DEFAULT_SESSION_OUTPUT_DIR),
    ("telebradesco_accounts_file", "meganet_content/telebradesco_accounts.json", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE),
    ("telebradesco_content_file", "meganet_content/telebradesco.json", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE),
)


def _normalized_path_text(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _matches_legacy_default_path(value: object, base_dir: pathlib.Path, legacy_relative: str) -> bool:
    text = _normalized_path_text(value)
    if not text:
        return True
    legacy_text = _normalized_path_text(legacy_relative)
    if text == legacy_text:
        return True
    try:
        candidate = pathlib.Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        return candidate.resolve() == (base_dir / pathlib.Path(legacy_relative)).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _same_file_contents(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _migrate_path_without_overwrite(source: pathlib.Path, destination: pathlib.Path) -> tuple[int, int]:
    """Move legacy data into the service directory without overwriting conflicts.

    Returns ``(moved, conflicts)``. Identical duplicate files are removed from the
    old layout, while conflicting files remain untouched for manual audit.
    """
    if not source.exists():
        return 0, 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        if destination.exists() and not destination.is_dir():
            logging.error("LAYOUT_MIGRATION_CONFLICT source=%s destination=%s", source, destination)
            return 0, 1
        destination.mkdir(parents=True, exist_ok=True)
        moved = conflicts = 0
        for child in list(source.iterdir()):
            child_moved, child_conflicts = _migrate_path_without_overwrite(child, destination / child.name)
            moved += child_moved
            conflicts += child_conflicts
        try:
            source.rmdir()
        except OSError:
            pass
        return moved, conflicts

    if not destination.exists():
        shutil.move(str(source), str(destination))
        logging.info("LAYOUT_MIGRATED source=%s destination=%s", source, destination)
        return 1, 0
    if _same_file_contents(source, destination):
        source.unlink()
        logging.info("LAYOUT_DUPLICATE_REMOVED source=%s destination=%s", source, destination)
        return 0, 0
    logging.error("LAYOUT_MIGRATION_CONFLICT source=%s destination=%s action=preserved-both", source, destination)
    return 0, 1


def migrate_legacy_service_layout(
    config_path: pathlib.Path,
    config: dict,
    explicit_cli: Optional[set[str]] = None,
) -> dict:
    """Migrate only the former built-in paths to the current service layout.

    User-defined paths and command-line overrides are never changed. Existing
    destination files are never overwritten; conflicts are retained in both
    locations and logged for manual review.
    """
    base = pathlib.Path(config_path).expanduser().resolve().parent
    explicit = set(explicit_cli or ())
    updated = dict(config or {})
    eligible: set[str] = set()

    for key, (legacy_relative, new_relative) in LEGACY_DEFAULT_CONFIG_PATHS.items():
        if key in explicit:
            continue
        current = updated.get(key)
        if key == "mail_inbox" and current in (None, ""):
            # Null means "use the inbox inside mail_store_dir" and must remain null.
            continue
        if _matches_legacy_default_path(current, base, legacy_relative):
            eligible.add(key)
            if current not in (None, "") or key in updated:
                updated[key] = new_relative.as_posix()

    moved = conflicts = 0
    for key, legacy_relative, new_relative in LEGACY_LAYOUT_ARTIFACTS:
        if key not in eligible:
            continue
        item_moved, item_conflicts = _migrate_path_without_overwrite(
            base / pathlib.Path(legacy_relative),
            base / new_relative,
        )
        moved += item_moved
        conflicts += item_conflicts

    old_content_dir = base / "meganet_content"
    try:
        old_content_dir.rmdir()
    except OSError:
        pass
    if moved or conflicts:
        logging.info("LAYOUT_MIGRATION_SUMMARY moved=%d conflicts=%d base=%s", moved, conflicts, base)
    return updated


def read_config_file(path: pathlib.Path) -> dict:
    if not path or not path.exists():
        return {}
    data = read_json_dict(path, default={}, log_prefix="CONFIG")
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("server"), dict):
        merged = data.get("server", {}).copy()
        # Legacy compatibility: older packages could keep content at the top level.
        for key in (*CONTENT_FILE_FIELDS, "news_route_map"):
            if key in data:
                merged[key] = data[key]
        return merged
    return data


def current_config_from_args(args: argparse.Namespace) -> dict:
    cfg = default_operational_config()
    for key in sorted(CONFIG_MANAGED_FIELDS):
        if hasattr(args, key):
            value = getattr(args, key)
            cfg[key] = _config_path_value(args, value) if key in PATH_FIELDS else _json_ready(value)
    return {key: _json_ready(cfg[key]) for key in sorted(CONFIG_MANAGED_FIELDS) if key in cfg}


def write_config_file(path: pathlib.Path, args_or_config: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(args_or_config, argparse.Namespace):
        data = current_config_from_args(args_or_config)
    elif isinstance(args_or_config, dict):
        data = default_operational_config()
        for key, value in args_or_config.items():
            if key in CONFIG_MANAGED_FIELDS:
                data[key] = _json_ready(value)
        data = {key: _json_ready(data[key]) for key in sorted(CONFIG_MANAGED_FIELDS) if key in data}
    else:
        data = current_config_from_args(argparse.Namespace(**default_operational_config()))
    atomic_write_json(path, data, ensure_ascii=False, indent=2)


def apply_config_to_args(args: argparse.Namespace, config: dict, explicit: set[str]) -> None:
    for key, value in config.items():
        if key not in CONFIG_MANAGED_FIELDS or key in explicit:
            continue
        try:
            setattr(args, key, _coerce_config_value(key, value))
        except (TypeError, ValueError, OSError) as exc:
            logging.warning("CONFIG_VALUE_IGNORED key=%s value=%r error=%s", key, value, exc)
