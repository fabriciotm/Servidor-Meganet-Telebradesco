from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Protocol, runtime_checkable



@runtime_checkable
class AsyncByteWriter(Protocol):
    """Structural type shared by TCP and serial session writers."""

    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...
    def is_closing(self) -> bool: ...
    def get_extra_info(self, name: str, default: object = None) -> object: ...


def format_datetime(value: object) -> str:
    """Format an ISO-like timestamp for human-facing administrative views.

    Kept outside the admin package so runtime/statistics can use it without
    introducing a dependency from infrastructure back into the HTTP layer.
    """
    text = str(value or "").strip()
    if not text:
        return "-"
    candidate = text.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(candidate)
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        pass
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", text)
    if match:
        y, m, d, hh, mm = match.groups()
        return f"{d}/{m}/{y} {hh}:{mm}"
    return text[:16]


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()

async def _close_client_writer(writer: AsyncByteWriter, peer: str, service: str) -> None:
    """Close a stream without surfacing normal Windows reset errors."""
    try:
        writer.close()
    except (ConnectionError, OSError) as exc:
        logging.debug("%s %s close ignored: %s", peer, service, exc)
        return
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError) as exc:
        logging.debug("%s %s wait_closed ignored: %s", peer, service, exc)
