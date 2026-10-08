"""Startup/shutdown orchestration for TCP, serial, adapter and admin services."""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Callable, Optional, Any

from ..telebradesco.accounts import (
    TelebradescoAccountsError,
    normalize_telebradesco_accounts_content_path,
    read_telebradesco_accounts,
)
from ..telebradesco.adapter import handle_telebradesco_adapter_client
from .configuration import (
    normalize_bind_host,
    service_args,
    service_display_name,
    typed_config,
    validate_dual_endpoints,
)
from .serial import serial_server_loop
from .statistics import UnifiedServiceRuntime
from .transport import handle_client
from .unified import handle_unified_tcp_client


async def run_services(
    args: argparse.Namespace,
    *,
    admin_server_factory: Callable[[argparse.Namespace], Optional[Any]],
) -> None:
    app_config = typed_config(args)
    args._service_runtime = UnifiedServiceRuntime(asyncio.get_running_loop())
    validate_dual_endpoints(args)

    accounts_content_path = normalize_telebradesco_accounts_content_path(app_config.telebradesco.content_file)
    accounts_tele_path = app_config.telebradesco.accounts_file
    accounts_content_path.parent.mkdir(parents=True, exist_ok=True)
    accounts_tele_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        read_telebradesco_accounts(
            accounts_tele_path,
            accounts_content_file=accounts_content_path,
            create_if_missing=True,
        )
    except TelebradescoAccountsError as exc:
        logging.critical("TELE_ACCOUNTS_STARTUP_UNAVAILABLE error=%s", exc)

    admin_httpd = admin_server_factory(args)
    tasks: list[asyncio.Task] = []
    servers: list[asyncio.AbstractServer] = []
    try:
        if app_config.telebradesco.adapter.enabled:
            adapter_host = normalize_bind_host(app_config.telebradesco.adapter.host)
            adapter_port = app_config.telebradesco.adapter.port
            adapter_server = await asyncio.start_server(
                lambda r, w, root=args: handle_telebradesco_adapter_client(r, w, root),
                host=adapter_host,
                port=adapter_port,
            )
            servers.append(adapter_server)
            addrs = ", ".join(str(sock.getsockname()) for sock in adapter_server.sockets or [])
            logging.info(
                "Telebradesco Asterisk adapter listening on %s diagnostics=%s",
                addrs,
                app_config.telebradesco.adapter.log_file,
            )
            tasks.append(asyncio.create_task(adapter_server.serve_forever(), name="TelebradescoAsteriskAdapter"))
        else:
            logging.info("Telebradesco Asterisk adapter disabled by configuration")

        tcp_endpoints: dict[tuple[str, int], list[tuple[str, argparse.Namespace]]] = {}
        for service in ("meganet", "telebradesco"):
            svc_args = service_args(args, service)
            mode = svc_args.server_transport
            display = service_display_name(service)
            if mode == "off":
                logging.info("%s disabled by configuration", display)
                continue
            if mode in {"tcp", "both"}:
                bind_host = normalize_bind_host(svc_args.host)
                svc_args.host = bind_host
                tcp_endpoints.setdefault((bind_host.casefold(), int(svc_args.port)), []).append((service, svc_args))
            if mode in {"serial", "both"}:
                tasks.append(asyncio.create_task(serial_server_loop(svc_args), name=f"{display}SerialServer"))

        for (_host_key, port), items in tcp_endpoints.items():
            bind_host = normalize_bind_host(items[0][1].host)
            if len(items) > 1:
                names = ", ".join(service_display_name(service) for service, _sa in items)
                server = await asyncio.start_server(
                    lambda r, w, root=args: handle_unified_tcp_client(r, w, root),
                    host=bind_host,
                    port=port,
                )
                servers.append(server)
                addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
                logging.info("TCP unificado listening on %s services=%s", addrs, names)
                tasks.append(asyncio.create_task(server.serve_forever(), name="UnifiedTCPServer"))
            else:
                service, svc_args = items[0]
                display = service_display_name(service)
                server = await asyncio.start_server(
                    lambda r, w, sa=svc_args: handle_client(r, w, sa),
                    host=bind_host,
                    port=port,
                )
                servers.append(server)
                addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
                logging.info("%s TCP listening on %s", display, addrs)
                tasks.append(asyncio.create_task(server.serve_forever(), name=f"{display}TCPServer"))

        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            # Wait for cancellation handlers to release resources (notably
            # serial ports) before the orchestrator returns or re-raises a
            # listener failure.  return_exceptions preserves the original
            # exception/cancellation that brought us into this cleanup path.
            await asyncio.gather(*tasks, return_exceptions=True)
        for server in servers:
            server.close()
        for server in servers:
            await server.wait_closed()
        if admin_httpd is not None:
            admin_httpd.shutdown()
            admin_httpd.server_close()
