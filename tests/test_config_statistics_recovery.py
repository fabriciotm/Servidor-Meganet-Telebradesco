from __future__ import annotations

import asyncio
import datetime as dt
import json

from meganet_server import api
from meganet_server import config as config_module
from meganet_server.runtime import statistics as rs


def _write_config(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_json_operational_choices_are_normalized_before_runtime_and_persistence(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    _write_config(config_path, {
        "meganet_transport": "BROKEN",
        "telebradesco_transport": "invalid",
        "meganet_auth_mode": "trust-everyone",
        "meganet_password_case": "surprise",
        "registration_duplicate_action": "overwrite-anything",
        "codigo_acesso_duplicate_action": "duplicate-anything",
        "transaction_close": "sometimes",
        "unknown_token_action": "execute",
        "raw_hayes_in_data_action": "maybe",
        "routing_mode": "random",
        "content_profile": "unknown-profile",
    })

    args = api.parse_args(["--config", str(config_path)])

    assert args.meganet_transport == "off"
    assert args.telebradesco_transport == "off"
    assert args.meganet_auth_mode == "require-known"
    assert args.meganet_password_case == "case-insensitive"
    assert args.registration_duplicate_action == "reject-message"
    assert args.codigo_acesso_duplicate_action == "reject-message"
    assert args.transaction_close == "off"
    assert args.unknown_token_action == "message-ready"
    assert args.raw_hayes_in_data_action == "ok-close"
    assert args.routing_mode == "unified"
    assert args.content_profile == "tectoy-news"

    assert args.app_config.meganet.endpoint.transport == args.meganet_transport
    assert args.app_config.telebradesco.endpoint.transport == args.telebradesco_transport

    persisted = config_module.read_config_file(config_path)
    for key in (
        "meganet_transport",
        "telebradesco_transport",
        "meganet_auth_mode",
        "meganet_password_case",
        "registration_duplicate_action",
        "codigo_acesso_duplicate_action",
        "transaction_close",
        "unknown_token_action",
        "raw_hayes_in_data_action",
        "routing_mode",
        "content_profile",
    ):
        assert persisted[key] == getattr(args, key)


def test_endpoint_ranges_are_identical_in_namespace_typed_view_and_persistence(tmp_path):
    config_path = tmp_path / "meganet_config.json"
    _write_config(config_path, {
        "meganet_port": 70000,
        "telebradesco_port": 0,
        "meganet_serial_baud": 1,
        "telebradesco_serial_baud": 5000000,
    })

    args = api.parse_args(["--config", str(config_path)])

    assert args.meganet_port == args.app_config.meganet.endpoint.port == 65535
    assert args.telebradesco_port == args.app_config.telebradesco.endpoint.port == 1
    assert args.meganet_serial_baud == args.app_config.meganet.endpoint.serial_baud == 300
    assert args.telebradesco_serial_baud == args.app_config.telebradesco.endpoint.serial_baud == 4_000_000

    persisted = config_module.read_config_file(config_path)
    assert persisted["meganet_port"] == 65535
    assert persisted["telebradesco_port"] == 1
    assert persisted["meganet_serial_baud"] == 300
    assert persisted["telebradesco_serial_baud"] == 4_000_000


def test_explicit_cli_endpoint_ranges_use_same_normalization_as_typed_view(tmp_path):
    config_path = tmp_path / "cli-ranges.json"
    args = api.parse_args([
        "--config", str(config_path),
        "--meganet-port", "70000",
        "--telebradesco-port", "0",
        "--meganet-serial-baud", "1",
        "--telebradesco-serial-baud", "5000000",
    ])

    assert args.meganet_port == args.app_config.meganet.endpoint.port == 65535
    assert args.telebradesco_port == args.app_config.telebradesco.endpoint.port == 1
    assert args.meganet_serial_baud == args.app_config.meganet.endpoint.serial_baud == 300
    assert args.telebradesco_serial_baud == args.app_config.telebradesco.endpoint.serial_baud == 4_000_000


def test_dashboard_keeps_newer_in_memory_stats_when_persistence_temporarily_fails(tmp_path, monkeypatch):
    access = tmp_path / "access"
    traffic = tmp_path / "traffic"
    access.mkdir()
    traffic.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic)

    current = dt.datetime(2026, 10, 4, 12, 0, 0)
    monkeypatch.setattr(rs, "_runtime_now_local", lambda: current)
    monkeypatch.setattr(
        rs.UnifiedServiceRuntime,
        "_minute_key",
        staticmethod(lambda now=None: (now or current).strftime("%Y-%m-%d %H:%M")),
    )

    (traffic / "traffic_20261004.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": "20261004",
        "totals": {"meganet": {"tx": 100, "rx": 0}, "telebradesco": {"tx": 0, "rx": 0}},
        "buckets": [{
            "key": "2026-10-04 12:00",
            "label": "12:00",
            "meganet_tx": 100,
            "meganet_rx": 0,
            "telebradesco_tx": 0,
            "telebradesco_rx": 0,
        }],
        "updated_at": "",
    }), encoding="utf-8")
    (access / "access_20261004.json").write_text(json.dumps({
        "schema": "runtime_access_daily_v1",
        "date": "20261004",
        "connections": [],
        "events": [{
            "time": "2026-10-04T11:59:00",
            "service": "meganet",
            "peer": "old:1",
            "identity": "OLD",
            "event": "Conexão iniciada",
            "tx_bytes": 0,
            "rx_bytes": 0,
        }],
        "updated_at": "",
    }), encoding="utf-8")

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())

    # Simula indisponibilidade temporária de escrita: a camada de persistência
    # registra a falha e o runtime continua atualizando o estado em memória.
    def fail_atomic_write(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(rs, "atomic_write_json", fail_atomic_write)

    writer = object()
    runtime.register(writer, "meganet", "new:2")
    runtime.record_traffic_for_writer(writer, "meganet", "tx", 50)

    snapshot = runtime.dashboard_snapshot()
    assert snapshot["traffic"]["meganet"]["tx"] == 150
    assert snapshot["recent_accesses"][0]["peer"] == "new:2"
