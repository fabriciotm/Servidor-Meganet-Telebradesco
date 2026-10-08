from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from meganet_server import api
from meganet_server import config as config_module
from meganet_server.runtime import unified as ru


def _write_config(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_explicit_no_telebradesco_adapter_wins_over_json(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    _write_config(config_path, {"telebradesco_adapter_enabled": True})

    args = api.parse_args([
        "--config", str(config_path),
        "--no-telebradesco-adapter",
    ])

    assert args.telebradesco_adapter_enabled is False
    assert args.app_config.telebradesco.adapter.enabled is False
    assert config_module.read_config_file(config_path)["telebradesco_adapter_enabled"] is False


def test_config_log_level_is_normalized_and_invalid_value_is_ignored(tmp_path):
    lower_path = tmp_path / "lower.json"
    _write_config(lower_path, {"log_level": "debug"})
    lower = api.parse_args(["--config", str(lower_path)])
    assert lower.log_level == "DEBUG"
    assert config_module.read_config_file(lower_path)["log_level"] == "DEBUG"

    invalid_path = tmp_path / "invalid.json"
    _write_config(invalid_path, {"log_level": "TRACE"})
    invalid = api.parse_args(["--config", str(invalid_path)])
    assert invalid.log_level == "INFO"
    assert config_module.read_config_file(invalid_path)["log_level"] == "INFO"


def test_admin_port_config_is_normalized_before_runtime_and_persistence(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    _write_config(config_path, {"admin_port": 99999})

    args = api.parse_args(["--config", str(config_path)])

    assert args.admin_port == 65535
    assert args.app_config.admin.port == 65535
    assert config_module.read_config_file(config_path)["admin_port"] == 65535


def test_admin_port_explicit_cli_uses_same_normalization_as_typed_view(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    args = api.parse_args([
        "--config", str(config_path),
        "--admin-port", "99999",
    ])

    assert args.admin_port == 65535
    assert args.app_config.admin.port == 65535
    assert config_module.read_config_file(config_path)["admin_port"] == 65535


def test_prepend_reader_readexactly_reports_full_partial_payload():
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"XY")
        reader.feed_eof()
        replay = ru._PrependAsyncReader(b"TBA1", reader)

        with pytest.raises(asyncio.IncompleteReadError) as captured:
            await replay.readexactly(8)

        assert captured.value.expected == 8
        assert captured.value.partial == b"TBA1XY"

    asyncio.run(scenario())


def test_unified_adapter_dispatch_replays_magic_and_tail(monkeypatch):
    async def scenario() -> None:
        captured = {}

        async def fake_adapter(reader, _writer, args):
            parts = []
            while True:
                chunk = await reader.read(32)
                if not chunk:
                    break
                parts.append(chunk)
            captured["payload"] = b"".join(parts)
            captured["same_args"] = args is root_args

        monkeypatch.setattr(ru, "handle_telebradesco_adapter_client", fake_adapter)
        root_args = argparse.Namespace(
            unified_tcp_detect_timeout=0.2,
            unified_tcp_default_service="meganet",
        )

        class Writer:
            def get_extra_info(self, name, default=None):
                return ("127.0.0.1", 45678) if name == "peername" else default

        reader = asyncio.StreamReader()
        reader.feed_data(b"TBA1TAIL")
        reader.feed_eof()
        await ru.handle_unified_tcp_client(reader, Writer(), root_args)

        assert captured == {"payload": b"TBA1TAIL", "same_args": True}

    asyncio.run(scenario())


def test_unified_detect_timeout_is_bounded_for_config_and_cli(tmp_path):
    config_path = tmp_path / "bounded.json"
    _write_config(config_path, {"unified_tcp_detect_timeout": 3600})
    from_config = api.parse_args(["--config", str(config_path)])
    assert from_config.unified_tcp_detect_timeout == 30.0
    assert from_config.app_config.listener.detect_timeout == 30.0

    cli_path = tmp_path / "bounded-cli.json"
    from_cli = api.parse_args([
        "--config", str(cli_path),
        "--unified-tcp-detect-timeout", "3600",
    ])
    assert from_cli.unified_tcp_detect_timeout == 30.0
    assert from_cli.app_config.listener.detect_timeout == 30.0


def test_unified_empty_probe_closes_without_dispatch(monkeypatch):
    async def scenario() -> None:
        dispatched = False

        async def fake_handle(*_args, **_kwargs):
            nonlocal dispatched
            dispatched = True

        monkeypatch.setattr(ru, "handle_client", fake_handle)

        class Writer:
            def __init__(self):
                self.closed = False
            def get_extra_info(self, name, default=None):
                return ("127.0.0.1", 45678) if name == "peername" else default
            def close(self):
                self.closed = True
            async def wait_closed(self):
                return None

        reader = asyncio.StreamReader()
        reader.feed_eof()
        writer = Writer()
        await ru.handle_unified_tcp_client(
            reader,
            writer,
            argparse.Namespace(unified_tcp_detect_timeout=0.2, unified_tcp_default_service="meganet"),
        )
        assert dispatched is False
        assert writer.closed is True

    asyncio.run(scenario())


def test_unified_bare_cr_dispatches_meganet_and_replays_signal(monkeypatch):
    async def scenario() -> None:
        captured = {}

        def fake_service_args(_args, service):
            return argparse.Namespace(active_service=service)

        async def fake_handle(reader, _writer, service_args):
            captured["service"] = service_args.active_service
            captured["payload"] = await reader.read(16)

        monkeypatch.setattr(ru, "_service_args", fake_service_args)
        monkeypatch.setattr(ru, "handle_client", fake_handle)

        class Writer:
            def get_extra_info(self, name, default=None):
                return ("127.0.0.1", 45678) if name == "peername" else default

        reader = asyncio.StreamReader()
        reader.feed_data(b"\r")
        reader.feed_eof()
        await ru.handle_unified_tcp_client(
            reader,
            Writer(),
            argparse.Namespace(unified_tcp_detect_timeout=0.2, unified_tcp_default_service="meganet"),
        )
        assert captured == {"service": "meganet", "payload": b"\r"}

    asyncio.run(scenario())


def test_unified_class1_dispatches_telebradesco_and_replays_command(monkeypatch):
    async def scenario() -> None:
        captured = {}

        def fake_service_args(_args, service):
            return argparse.Namespace(active_service=service)

        async def fake_handle(reader, _writer, service_args):
            captured["service"] = service_args.active_service
            parts = []
            while True:
                chunk = await reader.read(64)
                if not chunk:
                    break
                parts.append(chunk)
            captured["payload"] = b"".join(parts)

        monkeypatch.setattr(ru, "_service_args", fake_service_args)
        monkeypatch.setattr(ru, "handle_client", fake_handle)

        class Writer:
            def get_extra_info(self, name, default=None):
                return ("127.0.0.1", 45678) if name == "peername" else default

        command = b"AT+FRH=3\r"
        reader = asyncio.StreamReader()
        reader.feed_data(command)
        reader.feed_eof()
        await ru.handle_unified_tcp_client(
            reader,
            Writer(),
            argparse.Namespace(unified_tcp_detect_timeout=0.2, unified_tcp_default_service="meganet"),
        )
        assert captured == {"service": "telebradesco", "payload": command}

    asyncio.run(scenario())


def test_write_default_config_keeps_explicit_content_path_in_generated_config(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    content_path = tmp_path / "profiles" / "custom-content.json"

    with pytest.raises(SystemExit) as captured:
        api.parse_args([
            "--config", str(config_path),
            "--write-default-config",
            "--content-dir", str(content_path),
            "--admin-port", "22003",
        ])

    assert captured.value.code == 0
    persisted = config_module.read_config_file(config_path)
    assert persisted["content_dir"] == "profiles/custom-content.json"
    assert persisted["admin_port"] == 22003
    assert content_path.is_file()
