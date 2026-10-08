"""Runtime-facing configuration helpers for service listeners."""
from __future__ import annotations

import argparse
import logging

from ..config import AppConfig, normalize_bind_host, normalize_service_transport, refresh_app_config


def service_display_name(value: object) -> str:
    return "Telebradesco" if str(value or "").strip().lower() == "telebradesco" else "Mega Net"


def typed_config(args: argparse.Namespace) -> AppConfig:
    config = getattr(args, "app_config", None)
    if isinstance(config, AppConfig):
        return config
    return refresh_app_config(args)


def service_args(args: argparse.Namespace, service: str) -> argparse.Namespace:
    service = service.lower()
    clone = argparse.Namespace(**vars(args))
    clone.active_service = service
    endpoint = typed_config(args).endpoint(service)
    clone.server_transport = endpoint.transport
    clone.host = normalize_bind_host(endpoint.host)
    clone.port = endpoint.port
    clone.serial_port = endpoint.serial_port
    clone.serial_baud = endpoint.serial_baud
    clone._service_runtime = getattr(args, "_service_runtime", None)
    clone._root_args = args
    refs = getattr(args, "_service_arg_refs", None)
    if not isinstance(refs, dict):
        refs = {}
        setattr(args, "_service_arg_refs", refs)
    refs[service] = clone
    return clone


def validate_dual_endpoints(args: argparse.Namespace) -> None:
    services = [service_args(args, "meganet"), service_args(args, "telebradesco")]
    tcp_seen: dict[tuple[str, int], str] = {}
    serial_seen: dict[str, str] = {}
    enabled = 0
    for item in services:
        mode = item.server_transport
        label = service_display_name(item.active_service)
        if mode == "off":
            continue
        enabled += 1
        if mode in {"tcp", "both"}:
            if not (1 <= item.port <= 65535):
                raise ValueError(f"Porta TCP inválida para {label}: {item.port}")
            key = (item.host.casefold(), item.port)
            if key in tcp_seen:
                logging.info(
                    "TCP_UNIFIED_ENDPOINT enabled endpoint=%s:%s services=%s,%s",
                    item.host, item.port, tcp_seen[key], label,
                )
            else:
                tcp_seen[key] = label
        if mode in {"serial", "both"}:
            key = item.serial_port.strip().casefold()
            if not key:
                raise ValueError(f"Porta COM não informada para {label}")
            if key in serial_seen:
                raise ValueError(f"Conflito serial: {label} e {serial_seen[key]} usam {item.serial_port}")
            serial_seen[key] = label
    if enabled == 0:
        raise ValueError("Nenhum serviço está habilitado. Selecione TCP, serial ou both para ao menos um serviço.")


# Compatibility aliases kept for existing integrations.
_normalize_bind_host = normalize_bind_host
_normalize_service_transport = normalize_service_transport
_typed_config = typed_config
_service_args = service_args
_validate_dual_endpoints = validate_dual_endpoints
