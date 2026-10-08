"""Service dispatch with traffic accounting for MegaNet and Telebradesco."""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
from typing import Optional

from ..common import _close_client_writer
from ..meganet.transport import handle_meganet_client
from ..telebradesco.accounts import (
    DEFAULT_TELEBRADESCO_ACCOUNTS_FILE, DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE,
)
from ..telebradesco.content import (
    normalize_telebradesco_block_mode, normalize_telebradesco_error_hold_seconds,
)
from ..telebradesco.session import TelebradescoSession
from .statistics import UnifiedServiceRuntime

class _TrafficReader:
    def __init__(self, reader: object, runtime: Optional[UnifiedServiceRuntime], service: str, writer_ref: Optional[object] = None) -> None:
        self._reader = reader
        self._runtime = runtime
        self._service = service
        self._writer_ref = writer_ref

    async def read(self, n: int = 4096) -> bytes:
        data = await self._reader.read(n)
        if data and self._runtime:
            self._runtime.record_traffic_for_writer(self._writer_ref, self._service, "rx", len(data))
        return data

    def __getattr__(self, name: str):
        return getattr(self._reader, name)


class _TrafficWriter:
    def __init__(self, writer: object, runtime: Optional[UnifiedServiceRuntime], service: str, writer_ref: Optional[object] = None) -> None:
        self._writer = writer
        self._runtime = runtime
        self._service = service
        self._writer_ref = writer_ref

    def write(self, data: bytes) -> object:
        if data and self._runtime:
            self._runtime.record_traffic_for_writer(self._writer_ref, self._service, "tx", len(data))
        return self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()

    def get_extra_info(self, name: str, default=None):
        return self._writer.get_extra_info(name, default)

    def __getattr__(self, name: str):
        return getattr(self._writer, name)

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    service = str(getattr(args, "active_service", "meganet") or "meganet").strip().lower()
    if service not in {"meganet", "telebradesco"}:
        service = "meganet"
    runtime = getattr(args, "_service_runtime", None)
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "unknown"
    original_writer = writer
    if runtime:
        runtime.register(original_writer, service, peer)
    reader = _TrafficReader(reader, runtime, service, original_writer)
    writer = _TrafficWriter(writer, runtime, service, original_writer)
    logging.info("%s CONNECTION_DISPATCH service=%s", peer, service)
    try:
        if service == "telebradesco":
            session = TelebradescoSession(
                writer,
                peer=peer,
                accounts_file=pathlib.Path(getattr(args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE)),
                accounts_content_file=pathlib.Path(getattr(args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE)),
                block_mode=normalize_telebradesco_block_mode(getattr(args, "telebradesco_block_mode", "native-020")),
                error_hold_seconds=normalize_telebradesco_error_hold_seconds(
                    getattr(args, "telebradesco_error_hold_seconds", 2.0)
                ),
                access_identity_callback=(
                    (lambda identity: runtime.update_access_identity("telebradesco", peer, identity))
                    if runtime else None
                ),
            )
            await session.start()
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    await session.feed(data)
            except (ConnectionError, OSError) as exc:
                logging.info("%s Telebradesco connection closed by client: %s", peer, exc)
            finally:
                if session.close_task:
                    session.close_task.cancel()
                    await asyncio.gather(session.close_task, return_exceptions=True)
                await _close_client_writer(writer, peer, "Telebradesco")
                logging.info("%s Telebradesco disconnected", peer)
        else:
            await handle_meganet_client(reader, writer, args)
    finally:
        if runtime:
            runtime.unregister(original_writer, peer)
