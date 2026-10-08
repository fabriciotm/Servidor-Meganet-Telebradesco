from __future__ import annotations

import subprocess
import sys

import meganet_server
from meganet_server import api
from meganet_server import server
from meganet_server.meganet.accounts import CadastroDatabase as CadastroDatabaseImpl
from meganet_server.meganet.session import MegaNetSession as MegaNetSessionImpl
from meganet_server.telebradesco.session import TelebradescoSession as TelebradescoSessionImpl


def test_stable_api_exports_core_integrations() -> None:
    required = {
        "MegaNetSession",
        "TelebradescoSession",
        "CadastroDatabase",
        "MailStore",
        "ClientFrameParser",
        "compile_telebradesco_content",
        "decode_telebradesco_login_frame",
        "UnifiedServiceRuntime",
        "run_services",
        "main",
        "start_admin_server",
    }
    assert required <= set(api.__all__)
    assert api.MegaNetSession is MegaNetSessionImpl
    assert api.TelebradescoSession is TelebradescoSessionImpl
    assert api.CadastroDatabase is CadastroDatabaseImpl


def test_package_root_resolves_public_api_lazily() -> None:
    assert meganet_server.MegaNetSession is api.MegaNetSession
    assert meganet_server.compile_telebradesco_content is api.compile_telebradesco_content
    assert meganet_server.__version__ == api.__version__
    assert meganet_server.__version__ == "1.0.0"


def test_legacy_server_exports_are_preserved() -> None:
    # Public historical names and private helpers used by old diagnostics both
    # remain available through the compatibility facade.
    assert server.MegaNetSession is MegaNetSessionImpl
    assert server.TelebradescoSession is TelebradescoSessionImpl
    assert server.CadastroDatabase is CadastroDatabaseImpl
    assert callable(server._tele_reverse_bits8)
    assert callable(server._runtime_query_statistics)
    names = dir(server)
    assert "MegaNetSession" in names
    assert "_tele_reverse_bits8" in names


def test_legacy_session_import_is_lazy_in_fresh_process() -> None:
    code = r'''
import sys
import meganet_server.server as server
assert "MegaNetSession" not in server.__dict__
_ = server.MegaNetSession
assert "MegaNetSession" in server.__dict__
print("ok")
'''
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "ok"


def test_every_legacy_mapping_resolves() -> None:
    from meganet_server.compat import LEGACY_SERVER_EXPORTS, resolve_legacy_server_export
    assert len(LEGACY_SERVER_EXPORTS) >= 250
    for name in LEGACY_SERVER_EXPORTS:
        assert resolve_legacy_server_export(name) is not None, name


def test_moved_configuration_names_remain_available_through_legacy_server() -> None:
    import meganet_server.config as config_module
    import meganet_server.cli as cli_module

    assert server.PATH_FIELDS is config_module.PATH_FIELDS
    assert server.LEGACY_DEFAULT_CONFIG_PATHS is config_module.LEGACY_DEFAULT_CONFIG_PATHS
    assert server.default_operational_config is config_module.default_operational_config
    assert server.read_config_file is config_module.read_config_file
    assert server.write_config_file is config_module.write_config_file
    assert server.parse_args is cli_module.parse_args
    assert server.main is cli_module.main
