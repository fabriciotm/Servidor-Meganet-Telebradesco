"""Stable public API for MegaNet / Telebradesco integrations.

This module is intentionally small.  Code outside the project should prefer
these names (or the documented owning submodule) instead of importing private
helpers from :mod:`meganet_server.server`.
"""
from __future__ import annotations

from ._version import __version__
from .admin.server import start_admin_server
from .config import (
    AppConfig, AdminConfig, ListenerConfig, MegaNetConfig, TelebradescoConfig,
    default_operational_config, read_config_file, refresh_app_config, write_config_file,
)
from .cli import build_parser, main, parse_args
from .meganet.accounts import (
    CadastroDatabase,
    PasswordChangeStore,
    calculate_access_code_suffix,
    generate_access_code,
    generate_random_access_code,
    validate_access_code,
)
from .meganet.content import content_budget_report, normalize_content_record_lines
from .meganet.content_store import reload_active_meganet_content
from .meganet.mail import MailStore
from .meganet.protocol import (
    ClientFrameParser,
    DecodedFrame,
    encode_server_frame,
    parse_meganet_id_payload,
    parse_password_change_payload,
)
from .meganet.session import MegaNetPacketDeliveryError, MegaNetSession
from .runtime.configuration import service_args, service_display_name, validate_dual_endpoints
from .runtime.orchestration import run_services
from .runtime.statistics import UnifiedServiceRuntime
from .runtime.unified import handle_unified_tcp_client
from .telebradesco.accounts import (
    TelebradescoAccountsError,
    authenticate_telebradesco_login,
    read_telebradesco_accounts,
    record_telebradesco_account_access,
    update_telebradesco_accounts,
)
from .telebradesco.adapter import handle_telebradesco_adapter_client
from .telebradesco.content import (
    compile_telebradesco_content,
    compile_telebradesco_status,
    validate_telebradesco_content,
)
from .telebradesco.protocol import decode_telebradesco_login_frame
from .telebradesco.session import TelebradescoSession



__all__ = [
    "__version__",
    "AdminConfig",
    "AppConfig",
    "CadastroDatabase",
    "ClientFrameParser",
    "DecodedFrame",
    "ListenerConfig",
    "MailStore",
    "MegaNetConfig",
    "MegaNetPacketDeliveryError",
    "MegaNetSession",
    "PasswordChangeStore",
    "TelebradescoAccountsError",
    "TelebradescoConfig",
    "TelebradescoSession",
    "UnifiedServiceRuntime",
    "authenticate_telebradesco_login",
    "build_parser",
    "calculate_access_code_suffix",
    "compile_telebradesco_content",
    "compile_telebradesco_status",
    "content_budget_report",
    "decode_telebradesco_login_frame",
    "default_operational_config",
    "encode_server_frame",
    "generate_access_code",
    "generate_random_access_code",
    "handle_telebradesco_adapter_client",
    "handle_unified_tcp_client",
    "main",
    "normalize_content_record_lines",
    "parse_args",
    "parse_meganet_id_payload",
    "parse_password_change_payload",
    "read_config_file",
    "read_telebradesco_accounts",
    "refresh_app_config",
    "record_telebradesco_account_access",
    "reload_active_meganet_content",
    "run_services",
    "service_args",
    "service_display_name",
    "start_admin_server",
    "update_telebradesco_accounts",
    "validate_access_code",
    "validate_dual_endpoints",
    "validate_telebradesco_content",
    "write_config_file",
]
