from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from meganet_server import api
from meganet_server import config as config_module
from meganet_server.config import AppConfig
from meganet_server import server as s


def test_app_config_from_operational_defaults():
    ns = argparse.Namespace(**s.default_operational_config())
    cfg = AppConfig.from_namespace(ns)
    assert cfg.meganet.endpoint.transport == "tcp"
    assert cfg.meganet.endpoint.port == 19777
    assert cfg.telebradesco.endpoint.port == 19779
    assert cfg.telebradesco.adapter.port == 19780
    assert cfg.admin.port == 19778
    assert cfg.listener.default_service == "meganet"
    assert cfg.meganet.packet_max_retries == 5


def test_service_args_uses_typed_endpoint():
    ns = argparse.Namespace(**s.default_operational_config())
    ns.app_config = AppConfig.from_namespace(ns)
    mega = s._service_args(ns, "meganet")
    tele = s._service_args(ns, "telebradesco")
    assert (mega.host, mega.port, mega.server_transport) == ("127.0.0.1", 19777, "tcp")
    assert (tele.host, tele.port, tele.server_transport) == ("127.0.0.1", 19779, "tcp")


def test_parse_args_attaches_typed_view(tmp_path):
    config = tmp_path / "config.json"
    args = s.parse_args([
        "--config", str(config),
        "--meganet-port", "21001",
        "--telebradesco-port", "21002",
        "--admin-port", "21003",
        "--telebradesco-adapter-port", "21004",
    ])
    assert isinstance(args.app_config, AppConfig)
    assert args.app_config.meganet.endpoint.port == 21001
    assert args.app_config.telebradesco.endpoint.port == 21002
    assert args.app_config.admin.port == 21003
    assert args.app_config.telebradesco.adapter.port == 21004



def test_config_helpers_are_owned_by_config_module():
    assert s.default_operational_config is config_module.default_operational_config
    assert s.read_config_file is config_module.read_config_file
    assert s.write_config_file is config_module.write_config_file
    assert s.apply_config_to_args is config_module.apply_config_to_args


def test_cli_explicit_value_wins_and_relative_paths_follow_config_dir(tmp_path):
    config_path = tmp_path / "etc" / "meganet_config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({
        "meganet_port": 20111,
        "content_dir": "relative-content",
        "cadastro_db": "data/cadastros.json",
    }), encoding="utf-8")

    args = api.parse_args([
        "--config", str(config_path),
        "--meganet-port", "20222",
    ])
    assert args.meganet_port == 20222
    assert args.app_config.meganet.endpoint.port == 20222
    assert args.content_dir == (config_path.parent / "relative-content").resolve()
    assert args.cadastro_db == (config_path.parent / "data/cadastros.json").resolve()

    persisted = config_module.read_config_file(config_path)
    assert persisted["content_dir"] == "relative-content"
    assert persisted["cadastro_db"] == "data/cadastros.json"
    assert persisted["meganet_port"] == 20222


def test_refresh_app_config_rebuilds_typed_view():
    ns = argparse.Namespace(**config_module.default_operational_config())
    first = config_module.refresh_app_config(ns)
    assert first.meganet.endpoint.port == 19777
    ns.meganet_port = 22001
    second = config_module.refresh_app_config(ns)
    assert second is ns.app_config
    assert second is not first
    assert second.meganet.endpoint.port == 22001


def test_api_parse_args_does_not_import_legacy_server(tmp_path):
    config_path = tmp_path / "isolated.json"
    code = """
import sys
from meganet_server import api
assert 'meganet_server.server' not in sys.modules
args = api.parse_args(['--config', CONFIG_PATH, '--meganet-port', '22345'])
assert args.app_config.meganet.endpoint.port == 22345
assert 'meganet_server.server' not in sys.modules
print('ok')
""".replace('CONFIG_PATH', repr(str(config_path)))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "ok"


def test_legacy_layout_migration_preserves_custom_paths(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    custom = {"content_dir": "custom-content", "cadastro_db": "custom/users.json"}
    migrated = config_module.migrate_legacy_service_layout(config_path, custom, set())
    assert migrated["content_dir"] == "custom-content"
    assert migrated["cadastro_db"] == "custom/users.json"


def test_build_parser_is_public_and_admin_ephemeral_port_is_preserved(tmp_path):
    parser = api.build_parser()
    parsed = parser.parse_args(["--admin-port", "0"])
    assert parsed.admin_port == 0
    args = api.parse_args(["--config", str(tmp_path / "port0.json"), "--admin-port", "0"])
    assert args.admin_port == 0
    assert args.app_config.admin.port == 0
