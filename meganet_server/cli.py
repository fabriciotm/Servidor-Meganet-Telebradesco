"""Command-line parsing and startup for the MegaNet / Telebradesco server."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
from collections.abc import Sequence

from .config import (
    _explicit_cli_keys,
    _resolve_runtime_paths,
    apply_config_to_args,
    migrate_legacy_service_layout,
    normalize_bind_host,
    normalize_endpoint_args,
    read_config_file,
    refresh_app_config,
    write_config_file,
)
from .meganet.accounts import DEFAULT_CADASTRO_DB, DEFAULT_SESSION_OUTPUT_DIR
from .meganet.content import (
    CONNECTION_PANEL_PRESETS,
    DEFAULT_CONTENT_DIR,
    MAX_MESSAGE_LINE_LEN,
    merge_content_records,
    normalize_message_lines,
)
from .meganet.content_store import (
    apply_content_to_args,
    content_file_path,
    default_content_config,
    read_content_file,
    write_content_file,
)
from .meganet.mail import DEFAULT_MAIL_STORE_DIR, DEFAULT_WELCOME_EMAIL_BODY, DEFAULT_WELCOME_EMAIL_SUBJECT
from .meganet.session import DEFAULT_PASSWORD_CHANGE_STORE_DIR
from .runtime.orchestration import run_services
from .telebradesco.accounts import (
    DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE,
    DEFAULT_TELEBRADESCO_ACCOUNTS_FILE,
    normalize_telebradesco_accounts_content_path,
)
from .telebradesco.adapter import DEFAULT_TELEBRADESCO_ADAPTER_LOG_FILE
from .telebradesco.content import normalize_telebradesco_block_mode, normalize_telebradesco_error_hold_seconds

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Servidor Meganet - Telebradesco")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("meganet_config.json"), help="arquivo JSON de configuração")
    parser.add_argument("--active-service", choices=["meganet", "telebradesco"], default="meganet", help="protocolo ativo na porta configurada")
    parser.add_argument("--meganet-transport", choices=["off", "tcp", "serial", "both"], default="tcp", help="transportes independentes do MegaNet")
    parser.add_argument("--meganet-host", default="127.0.0.1", help="host TCP do MegaNet; use 0.0.0.0 para todas as interfaces IPv4")
    parser.add_argument("--meganet-port", type=int, default=19777, help="porta TCP do MegaNet")
    parser.add_argument("--meganet-serial-port", default="COM12", help="porta COM do MegaNet")
    parser.add_argument("--meganet-serial-baud", type=int, default=115200, help="baud rate serial do MegaNet")
    parser.add_argument("--telebradesco-transport", choices=["off", "tcp", "serial", "both"], default="tcp", help="transportes independentes do Telebradesco")
    parser.add_argument("--telebradesco-host", default="127.0.0.1", help="host TCP do Telebradesco; use 0.0.0.0 para todas as interfaces IPv4")
    parser.add_argument("--telebradesco-port", type=int, default=19779, help="porta TCP do Telebradesco")
    parser.add_argument("--telebradesco-serial-port", default="COM10", help="porta COM do Telebradesco")
    parser.add_argument("--telebradesco-serial-baud", type=int, default=115200, help="baud rate serial do Telebradesco")
    parser.add_argument("--telebradesco-content-file", type=pathlib.Path, default=DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE, help="arquivo JSON de conteúdo financeiro por conta Telebradesco (accounts_content.json)")
    parser.add_argument("--telebradesco-accounts-file", type=pathlib.Path, default=DEFAULT_TELEBRADESCO_ACCOUNTS_FILE, help="banco JSON de identificação, senha, bloqueio e metadados das contas Telebradesco")
    parser.add_argument("--telebradesco-block-mode", choices=["native-020", "deny-login"], default="native-020", help="native-020 sends no account content, stores 020 in Conta Corrente and Poupanca slots, and omits sections 2/9; deny-login rejects immediately with ROM code 010")
    parser.add_argument("--telebradesco-error-hold-seconds", type=float, default=2.0, help="seconds to delay ATH0 OK for terminal Telebradesco authentication errors (0..5)")
    parser.add_argument("--telebradesco-adapter-enabled", dest="telebradesco_adapter_enabled", action="store_true", default=True, help="enable the internal binary adapter used by app_telebradesco")
    parser.add_argument("--no-telebradesco-adapter", dest="telebradesco_adapter_enabled", action="store_false", help="disable the internal app_telebradesco adapter")
    parser.add_argument("--telebradesco-adapter-host", default="127.0.0.1", help="bind host for the internal app_telebradesco adapter")
    parser.add_argument("--telebradesco-adapter-port", type=int, default=19780, help="TCP port for the internal app_telebradesco adapter")
    parser.add_argument("--telebradesco-adapter-timeout", type=float, default=5.0, help="adapter request timeout in seconds (0.2..30)")
    parser.add_argument("--telebradesco-adapter-log-file", type=pathlib.Path, default=DEFAULT_TELEBRADESCO_ADAPTER_LOG_FILE, help="persistent JSONL diagnostics for every app_telebradesco adapter request")
    parser.add_argument("--write-default-config", action="store_true", help="write a default operational configuration and exit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19777)
    parser.add_argument("--server-transport", choices=["tcp", "serial", "both"], default="tcp", help="transport used by the server: TCP, serial COM, or both")
    parser.add_argument("--serial-port", default="COM10", help="serial/COM port used when --server-transport serial/both is selected")
    parser.add_argument("--serial-baud", type=int, default=115200, help="serial baud rate used by --server-transport serial/both")
    parser.add_argument("--online-start", dest="online_start", action="store_true", default=True, help="start directly at login prompt, skipping Hayes AT handling; enabled by default")
    parser.add_argument("--no-online-start", dest="online_start", action="store_false", help="disable direct online start")
    parser.add_argument("--profile-idle", type=float, default=0.25, help="seconds of silence used to terminate the no-CR profile field")
    parser.add_argument("--post-dle-delay", type=float, default=0.20, help="seconds to wait after ACKing DLE before sending packetized #bemvindo#/#ready#")
    parser.add_argument("--enable-session-logs", dest="enable_session_logs", action="store_true", default=False, help="save per-session JSON/CSV files in --output-dir; disabled by default")
    parser.add_argument("--no-session-logs", dest="enable_session_logs", action="store_false", help="disable per-session JSON/CSV files; default behavior")
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_SESSION_OUTPUT_DIR)
    parser.add_argument("--content-dir", type=pathlib.Path, default=DEFAULT_CONTENT_DIR, help="directory containing the separate content.json file used by the Content tab")
    parser.add_argument("--cadastro-db", type=pathlib.Path, default=DEFAULT_CADASTRO_DB, help="persistent JSON database for received cadastro records")
    parser.add_argument("--auth-mode", choices=["off", "allow-new", "require-known"], default="off", help="legacy PPP/profile validation mode; keep off for normal Mega Net account validation")
    parser.add_argument("--auth-password-source", choices=["off", "bootstrap", "cadastro-senha", "either"], default="bootstrap", help="legacy PPP/profile password source, only used when --auth-mode is not off")
    parser.add_argument("--auth-check-bootstrap-login", action="store_true", help="legacy option: also require the bootstrap login string to match the one stored in the DB")
    parser.add_argument("--auth-reject-action", choices=["nak-close", "no-carrier-close", "silent-close"], default="nak-close", help="legacy PPP/profile rejection action")
    parser.add_argument("--meganet-auth-mode", choices=["off", "allow-new", "require-known"], default="require-known", help="Mega Net account validation using cadastro fields.nome_meganet and fields.senha")
    parser.add_argument("--meganet-reject-action", choices=["message-then-ready", "nak-close", "silent-close"], default="message-then-ready", help="what to do when Mega Net account validation fails")
    parser.add_argument("--meganet-password-case", choices=["exact", "upper", "lower", "case-insensitive"], default="case-insensitive", help="how to compare #id password with fields.senha; default handles ROM uppercasing such as 123abc -> 123ABC")
    parser.add_argument("--registration-duplicate-action", choices=["reject-message", "allow-update"], default="reject-message", help="what to do when the submitted nome_meganet already exists at #end cadastro#")
    parser.add_argument("--codigo-acesso-duplicate-action", choices=["reject-message", "allow"], default="reject-message", help="what to do when a new cadastro reuses an existing fields.codigo_acesso; default rejects the cadastro")
    parser.add_argument("--allow-duplicate-codigo-acesso", action="store_true", help="shortcut for --codigo-acesso-duplicate-action allow; useful only for testing")
    parser.add_argument("--transaction-close", choices=["after-final-message", "after-bye", "off"], default="off", help="close each client connection after the final response for one Mega Net activity; the server process keeps listening")
    parser.add_argument("--transaction-close-delay", type=float, default=0.35, help="seconds to wait after the final #ready# before closing the client socket")
    parser.add_argument("--post-id-action", choices=["none", "ready", "message-ready", "ready-close", "message-ready-close", "tectoy-news", "tectoy-news-close", "sync-batch", "sync-batch-close", "id-exact-end-wait", "id-exact-end-ready-wait", "id-exact-end-raw-ack-eot-wait", "id-exact-end-raw-eot-wait", "id-exact-end-raw-ack-wait", "id-body-hash-end-ready2-wait", "id-body-hash-end-ready1-wait", "id-body-hash-end-welcome-ready-wait", "id-body-hash-end-bemvindo-ready-wait", "id-exact-body-eot-wait", "id-exact-body-ack-eot-wait", "id-exact-body-ready-eot-wait", "id-exact-body-text-eot-wait", "id-exact-body-text-ack-eot-wait", "id-direct-send-news-wait", "id-direct-send-begin-news-wait", "id-exact-send-news-wait", "id-exact-send-begin-news-wait", "id-combined-empty-wait", "id-combined-empty-ready-wait", "id-combined-hash-empty-wait", "id-combined-empty-eot-wait", "id-combined-empty-raw-ack-eot-wait", "id-combined-empty-raw-eot-wait", "id-combined-empty-news-wait", "id-combined-empty-begin-news-wait", "id-exact-nocr-wait", "id-exact-nocr-ready-wait", "id-combined-nocr-wait", "id-combined-nocr-ready-wait", "close"], default="id-body-hash-end-ready2-wait", help="mapper option after successful #id auth; includes #message end# separator/status probes based on the ROM analysis")
    parser.add_argument("--post-id-close-delay", type=float, default=0.35, help="seconds to wait before forcibly closing the client socket for post-id close mapper actions")
    parser.add_argument("--post-id-message-delay", type=float, default=0.15, help="seconds to wait after #id ACK/auth before sending a service message block")
    parser.add_argument("--outbound-line-max-len", type=int, default=MAX_MESSAGE_LINE_LEN, help="maximum visible text characters per message line before wrapping/splitting (8..35; values above 35 are capped; 36 causes automatic ROM wrap plus CR double-advance)")
    parser.add_argument("--outbound-packet-delay", type=float, default=0.10, help="seconds to wait between outbound message packets for conservative ROM pacing")
    parser.add_argument("--meganet-packet-ack-mode", choices=["off", "transfers", "all"], default="all", help="ACK/NAK control for server packets: off, transfers (compatibility), or all (recommended)")
    parser.add_argument("--meganet-packet-ack-timeout", type=float, default=3.0, help="seconds to wait for cartridge ACK 0x06 before retransmitting")
    parser.add_argument("--meganet-packet-max-retries", type=int, default=5, help="maximum transmission attempts using the same frame and sequence number")
    parser.add_argument("--meganet-bootstrap-noise-tolerance", type=int, default=2, help="number of invalid bootstrap bytes discarded inside the configured window before closing; 0 restores strict first-byte close")
    parser.add_argument("--meganet-bootstrap-noise-window", type=float, default=2.0, help="seconds used to count invalid bootstrap bytes")
    parser.add_argument("--unknown-token-action", choices=["none", "ready", "message-ready"], default="message-ready", help="mapper option: how to respond to unmapped client tokens while capturing group commands")
    parser.add_argument("--raw-hayes-in-data-action", choices=["ignore", "close", "ok-close"], default="ok-close", help="what to do if ATH0/+++ leaks through the TCP bridge during packet/data mode")
    parser.add_argument("--raw-hayes-close-delay", type=float, default=0.20, help="seconds to wait after handling leaked ATH0/+++ before closing the TCP client")
    parser.add_argument("--tectoy-news-line", action="append", help="Tectoy News line to send after #id auth; can be repeated; {usuario} is replaced by the Mega Net user")
    parser.add_argument("--tectoy-news-file", type=pathlib.Path, help="text file with one Tectoy News line per row; {usuario} is replaced by the Mega Net user")
    parser.add_argument("--connection-panel-preset", choices=["unified", "default", "tectoy-news", "mail", "internet", "revista", "outros", "troca-senha"], default="unified", help="preset for the message shown in the ROM connection panel after #id auth; unified is neutral for the single production server")
    parser.add_argument("--connection-panel-mail-count", choices=["auto", "off"], default="auto", help="show pending mailbox count in the post-#id connection panel when placeholders are present; default auto")
    parser.add_argument("--service", choices=["tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"], help="legacy/manual shortcut for diagnostics; omit this in normal operation")
    parser.add_argument("--content-profile", choices=["tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"], default="tectoy-news", help="legacy/manual content set used only with --routing-mode manual or as fallback")
    parser.add_argument("--routing-mode", choices=["unified", "manual"], default="unified", help="unified routes #send news N# by token number; manual preserves the older --content-profile behavior")
    parser.add_argument("--news-route", action="append", help="override unified group routing, format N=profile, e.g. --news-route 1=tectoy-news --news-route 4=outros")
    parser.add_argument("--mail-store-dir", type=pathlib.Path, default=DEFAULT_MAIL_STORE_DIR, help="directory for persistent MegaNet mail files: inbox/outbox_email/outbox_sugestoes")
    parser.add_argument("--mail-inbox", type=pathlib.Path, help="optional custom inbox JSON file; default is meganet/mail/inbox.json")
    parser.add_argument("--mail-domain", default="meganet.tectoy.com.br", help="MegaNet internal e-mail domain; users receive <nome_meganet>@domain")
    parser.add_argument("--admin-email", default="admin@meganet.tectoy.com.br", help="server manager mailbox used as the MegaNet admin sender/recipient")
    parser.add_argument("--welcome-email-enabled", dest="welcome_email_enabled", action="store_true", default=True, help="send an internal MegaNet welcome e-mail after each accepted registration")
    parser.add_argument("--no-welcome-email", dest="welcome_email_enabled", action="store_false", help="do not create the internal MegaNet welcome e-mail after registration")
    parser.add_argument("--welcome-email-from", default="admin@meganet.tectoy.com.br", help="sender used for the internal welcome e-mail")
    parser.add_argument("--welcome-email-subject", default=DEFAULT_WELCOME_EMAIL_SUBJECT, help="subject used for the internal welcome e-mail; placeholders are supported")
    parser.add_argument("--welcome-email-body", default="\n".join(DEFAULT_WELCOME_EMAIL_BODY), help="body used for the internal welcome e-mail; placeholders are supported")
    parser.add_argument("--password-change-store-dir", type=pathlib.Path, default=DEFAULT_PASSWORD_CHANGE_STORE_DIR, help="directory for TROCA DE SENHA probe logs")
    parser.add_argument("--password-change-apply", dest="password_change_apply", action="store_true", default=True, help="update cadastro DB when the ROM sends #senha <user> <old> <new>; enabled by default")
    parser.add_argument("--no-password-change-apply", dest="password_change_apply", action="store_false", help="disable password-change persistence")
    parser.add_argument("--connection-panel-line", action="append", help="custom connection-panel line; can be repeated; overrides --connection-panel-preset")
    parser.add_argument("--connection-panel-file", type=pathlib.Path, help="text file with one connection-panel line per row; overrides --connection-panel-preset")
    parser.add_argument("--message", action="append", help="message line to show after cadastro; can be repeated; {usuario} is replaced by fields.nome_meganet")
    parser.add_argument("--message-file", type=pathlib.Path, help="text file with one message line per row")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--admin-web", dest="admin_web", action="store_true", default=True, help="enable lightweight web administration interface; default enabled")
    parser.add_argument("--no-admin-web", dest="admin_web", action="store_false", help="disable web administration interface")
    parser.add_argument("--admin-host", default="127.0.0.1", help="admin web bind host; use 0.0.0.0 para todas as interfaces IPv4")
    parser.add_argument("--admin-port", type=int, default=19778, help="admin web bind port")
    parser.add_argument("--unified-tcp-default-service", choices=["meganet", "telebradesco"], default="meganet", help="serviço usado como fallback quando uma porta TCP compartilhada não consegue identificar o protocolo a tempo")
    parser.add_argument("--unified-tcp-detect-timeout", type=float, default=3.0, help="tempo máximo, em segundos, para autodetectar MegaNet ou Telebradesco em porta TCP compartilhada")
    parser.add_argument("--meganet-auto-ready-timeout", type=float, default=12.0, help="tempo máximo para aguardar MGR1 após MGA1 antes de iniciar login por compatibilidade")
    return parser


def _read_configured_text_lines(path: pathlib.Path, label: str) -> list[str]:
    """Read a configured UTF-8 line file or fail startup without traceback."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Não foi possível ler {label} {path}: {exc}") from exc
    return normalize_message_lines(text.splitlines())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv_list)
    args.config = pathlib.Path(args.config).expanduser().resolve()
    args._config_base_dir = args.config.parent
    explicit_cli = _explicit_cli_keys(argv_list)
    loaded_config: dict = {}
    preserve_existing_config = False

    if args.write_default_config:
        normalize_endpoint_args(args)
        _resolve_runtime_paths(args, args.config.parent)
        write_config_file(args.config, args)
        content_path = write_content_file(args.content_dir, default_content_config())
        print(f"Configuração padrão gravada em {args.config}")
        print(f"Conteúdo padrão gravado em {content_path}")
        raise SystemExit(0)

    if args.config and args.config.exists():
        try:
            raw_config = json.loads(args.config.read_text(encoding="utf-8"))
            preserve_existing_config = not isinstance(raw_config, dict)
        except (OSError, UnicodeError, json.JSONDecodeError):
            preserve_existing_config = True
        loaded_config = read_config_file(args.config)
        loaded_config = migrate_legacy_service_layout(args.config, loaded_config, explicit_cli)
        apply_config_to_args(args, loaded_config, explicit_cli)
    elif args.config:
        loaded_config = migrate_legacy_service_layout(args.config, {}, explicit_cli)
        write_config_file(args.config, args)
    normalize_endpoint_args(args)
    _resolve_runtime_paths(args, args.config.parent)
    previous_tele_content_path = pathlib.Path(getattr(args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
    args.telebradesco_content_file = normalize_telebradesco_accounts_content_path(previous_tele_content_path).resolve()
    if args.telebradesco_content_file != previous_tele_content_path:
        logging.warning("TELE_CONTENT_CONFIG_RENAMED old=%s new=%s", previous_tele_content_path, args.telebradesco_content_file)

    args.active_service = str(getattr(args, "active_service", "meganet") or "meganet").strip().lower()
    if args.active_service not in {"meganet", "telebradesco"}:
        args.active_service = "meganet"
    args.unified_tcp_default_service = str(getattr(args, "unified_tcp_default_service", "meganet") or "meganet").strip().lower()
    if args.unified_tcp_default_service not in {"meganet", "telebradesco"}:
        args.unified_tcp_default_service = "meganet"
    try:
        args.unified_tcp_detect_timeout = max(0.2, min(30.0, float(
            getattr(args, "unified_tcp_detect_timeout", 3.0) or 3.0
        )))
    except (TypeError, ValueError):
        args.unified_tcp_detect_timeout = 3.0
    try:
        args.meganet_auto_ready_timeout = max(0.5, min(30.0, float(
            getattr(args, "meganet_auto_ready_timeout", 12.0) or 12.0
        )))
    except (TypeError, ValueError):
        args.meganet_auto_ready_timeout = 12.0
    args.telebradesco_block_mode = normalize_telebradesco_block_mode(getattr(args, "telebradesco_block_mode", "native-020"))
    args.telebradesco_error_hold_seconds = normalize_telebradesco_error_hold_seconds(
        getattr(args, "telebradesco_error_hold_seconds", 2.0)
    )
    args.telebradesco_adapter_host = normalize_bind_host(getattr(args, "telebradesco_adapter_host", "127.0.0.1"))
    try:
        args.admin_port = max(0, min(65535, int(getattr(args, "admin_port", 19778))))
    except (TypeError, ValueError):
        args.admin_port = 19778
    try:
        args.telebradesco_adapter_port = max(1, min(65535, int(getattr(args, "telebradesco_adapter_port", 19780) or 19780)))
    except (TypeError, ValueError):
        args.telebradesco_adapter_port = 19780
    try:
        args.telebradesco_adapter_timeout = max(0.2, min(30.0, float(getattr(args, "telebradesco_adapter_timeout", 5.0) or 5.0)))
    except (TypeError, ValueError):
        args.telebradesco_adapter_timeout = 5.0
    args.meganet_packet_ack_mode = str(
        getattr(args, "meganet_packet_ack_mode", "all") or "all"
    ).strip().lower()
    if args.meganet_packet_ack_mode not in {"off", "transfers", "all"}:
        args.meganet_packet_ack_mode = "all"
    try:
        args.meganet_packet_ack_timeout = max(
            0.20,
            min(30.0, float(getattr(args, "meganet_packet_ack_timeout", 3.0) or 3.0)),
        )
    except (TypeError, ValueError):
        args.meganet_packet_ack_timeout = 3.0
    try:
        args.meganet_packet_max_retries = max(
            1,
            min(10, int(getattr(args, "meganet_packet_max_retries", 5) or 5)),
        )
    except (TypeError, ValueError):
        args.meganet_packet_max_retries = 5
    try:
        args.meganet_bootstrap_noise_tolerance = max(
            0,
            min(32, int(getattr(args, "meganet_bootstrap_noise_tolerance", 2))),
        )
    except (TypeError, ValueError):
        args.meganet_bootstrap_noise_tolerance = 2
    try:
        args.meganet_bootstrap_noise_window = max(
            0.10,
            min(30.0, float(getattr(args, "meganet_bootstrap_noise_window", 2.0) or 2.0)),
        )
    except (TypeError, ValueError):
        args.meganet_bootstrap_noise_window = 2.0

    if getattr(args, "allow_duplicate_codigo_acesso", False):
        args.codigo_acesso_duplicate_action = "allow"

    if args.service:
        # Legacy diagnostic/manual mode. Normal production mode omits --service
        # and uses --routing-mode unified to route by #send news N#.
        args.routing_mode = "manual"
        if not args.connection_panel_line and not args.connection_panel_file:
            args.connection_panel_preset = args.service
        if args.service in ("tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"):
            args.content_profile = args.service
        if args.service == "troca-senha" and args.meganet_auth_mode == "require-known":
            logging.warning("--service troca-senha with --meganet-auth-mode require-known may reject #id before mapping the flow")

    default_news_route_map = {"1": "tectoy-news", "2": "internet", "3": "revista", "4": "outros"}
    current_route_map = getattr(args, "news_route_map", None)
    if isinstance(current_route_map, dict):
        args.news_route_map = {**default_news_route_map, **{str(k): str(v) for k, v in current_route_map.items()}}
    else:
        args.news_route_map = default_news_route_map.copy()
    if args.news_route:
        valid_profiles = {"tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"}
        for item in args.news_route:
            if "=" not in item:
                raise SystemExit(f"invalid --news-route {item!r}; expected N=profile")
            key, value = item.split("=", 1)
            key = key.strip()
            value = value.strip().lower()
            if not key or value not in valid_profiles:
                raise SystemExit(f"invalid --news-route {item!r}; profile must be one of {sorted(valid_profiles)}")
            args.news_route_map[key] = value

    # /.2: Mega Net content is read from its service-specific content directory. If the file does
    # not exist, migrate legacy content fields found in meganet_config.json.
    content_cfg = read_content_file(args.content_dir, legacy_config=loaded_config, create_if_missing=True)
    apply_content_to_args(args, content_cfg)

    if args.tectoy_news_file:
        args.tectoy_news_lines = _read_configured_text_lines(args.tectoy_news_file, "arquivo de Tectoy News")
    elif args.tectoy_news_line:
        args.tectoy_news_lines = normalize_message_lines(args.tectoy_news_line)

    if args.connection_panel_file:
        args.connection_panel_lines = _read_configured_text_lines(args.connection_panel_file, "arquivo do painel de conexão")
    elif args.connection_panel_line:
        args.connection_panel_lines = normalize_message_lines(args.connection_panel_line)
    elif "connection_panel_preset" in explicit_cli or args.service:
        args.connection_panel_lines = CONNECTION_PANEL_PRESETS.get(args.connection_panel_preset, CONNECTION_PANEL_PRESETS["default"])[:]

    if args.message_file:
        args.message_lines = _read_configured_text_lines(args.message_file, "arquivo de mensagens")
    elif args.message:
        args.message_lines = normalize_message_lines(args.message)

    args.content_records = merge_content_records(getattr(args, "content_records", None))

    try:
        write_content_file(args.content_dir, args)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("CONTENT_WRITE_FAILED path=%s error=%s", content_file_path(args.content_dir), exc)
    if getattr(args, "config", None):
        if preserve_existing_config:
            logging.warning("CONFIG_WRITE_SKIPPED_INVALID_EXISTING path=%s", args.config)
        else:
            try:
                write_config_file(pathlib.Path(args.config), args)
            except (OSError, TypeError, ValueError) as exc:
                logging.warning("CONFIG_WRITE_FAILED path=%s error=%s", args.config, exc)
    # Attach the typed view only after legacy normalization and persistence.
    # paths complete, so existing JSON/CLI behavior remains unchanged.
    refresh_app_config(args)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Import lazily so configuration parsing remains lightweight while
    # the executable path depends directly on the owning admin module, not the
    # legacy compatibility facade.
    from .admin.server import start_admin_server

    try:
        asyncio.run(run_services(args, admin_server_factory=start_admin_server))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        logging.critical("STARTUP_FAILED error=%s", exc)
        return 1
    return 0
