from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import pathlib
import struct
import threading

from ..common import _close_client_writer, utc_now_iso
from ..storage import append_jsonl
from .accounts import (
    DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE, DEFAULT_TELEBRADESCO_ACCOUNTS_FILE,
    TelebradescoAccountsError, authenticate_telebradesco_login, mask_telebradesco_login_text,
    read_telebradesco_accounts, record_telebradesco_account_access,
)
from .content import (
    TELEBRADESCO_FRAME_PAYLOAD_MAX, _tele_ascii, compile_telebradesco_content,
    compile_telebradesco_status, normalize_telebradesco_block_mode,
)

TELEBRADESCO_ADAPTER_REQUEST_MAGIC = b"TBA1"
TELEBRADESCO_ADAPTER_RESPONSE_MAGIC = b"TBR1"
TELEBRADESCO_ADAPTER_VERSION = 1
TELEBRADESCO_ADAPTER_REQUEST_HEADER = struct.Struct("!4sBBH")
TELEBRADESCO_ADAPTER_RESPONSE_HEADER = struct.Struct("!4sBBHIHH")
TELEBRADESCO_ADAPTER_MAX_LOGIN = 96
TELEBRADESCO_ADAPTER_MAX_FRAMES = 254
TELEBRADESCO_ADAPTER_MAX_TEXT = 255
DEFAULT_TELEBRADESCO_ADAPTER_LOG_FILE = pathlib.Path("logs") / "telebradesco_adapter.jsonl"
TELEBRADESCO_ADAPTER_LOG_LOCK = threading.RLock()
def _telebradesco_adapter_mask_login(login_text: object) -> str:
    return mask_telebradesco_login_text(login_text)
def _telebradesco_adapter_ascii_preview(data: bytes, limit: int = 160) -> str:
    """Return a single-line printable preview for Telebradesco adapter logs."""
    raw = bytes(data or b"")[:max(0, int(limit or 0))]
    preview = "".join(chr(value) if 0x20 <= value <= 0x7E else "." for value in raw)
    if len(data or b"") > len(raw):
        preview += "..."
    return preview
def _telebradesco_adapter_wire_descriptor(frame: bytes, index: int) -> dict:
    payload = bytes(frame)
    sequence = 2 + int(index)
    prefix = bytes([0x10, 0x80, sequence & 0xFF, len(payload) & 0xFF]) + payload
    checksum = sum(prefix) & 0xFFFF
    return {
        "frame": index + 1,
        "sequence": sequence & 0xFF,
        "payload_length": len(payload),
        "useful_length": max(0, len(payload) - 1),
        "checksum": f"{checksum:04X}",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_hex": payload.hex(" ").upper(),
        "wire_hex": (prefix + checksum.to_bytes(2, "big")).hex(" ").upper(),
    }
def _telebradesco_adapter_write_log(args: argparse.Namespace, payload: dict) -> None:
    path = pathlib.Path(
        getattr(args, "telebradesco_adapter_log_file", DEFAULT_TELEBRADESCO_ADAPTER_LOG_FILE)
        or DEFAULT_TELEBRADESCO_ADAPTER_LOG_FILE
    )
    record = {"at": utc_now_iso(), **dict(payload or {})}
    try:
        with TELEBRADESCO_ADAPTER_LOG_LOCK:
            append_jsonl(path, record, fsync=True)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("TELE_ADAPTER_FILE_LOG_FAILED path=%s error=%s", path, exc)
async def _telebradesco_adapter_send_error(
    writer: asyncio.StreamWriter,
    *,
    result_code: int,
    status: str,
    peer: str = "unknown",
) -> None:
    status_raw = _tele_ascii(status)[:TELEBRADESCO_ADAPTER_MAX_TEXT].encode("ascii", "replace")
    header = TELEBRADESCO_ADAPTER_RESPONSE_HEADER.pack(
        TELEBRADESCO_ADAPTER_RESPONSE_MAGIC,
        TELEBRADESCO_ADAPTER_VERSION,
        int(result_code) & 0xFF,
        0,
        0,
        0,
        len(status_raw),
    )
    wire = header + status_raw
    logging.info(
        "%s TELE_ADAPTER_TX_ERROR result=%d status=%s bytes=%d",
        peer, int(result_code) & 0xFF, status, len(wire),
    )
    logging.debug(
        "%s TELE_ADAPTER_TX_ERROR_WIRE hex=%s ascii=%s",
        peer, wire.hex(" ").upper(), _telebradesco_adapter_ascii_preview(wire),
    )
    writer.write(wire)
    await writer.drain()
async def handle_telebradesco_adapter_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    args: argparse.Namespace,
) -> None:
    """Serve one bounded app_telebradesco backend request.

    Request (network byte order): ``TBA1, version, flags, login_len, login``.
    Response: ``TBR1, version, result, frame_count, stream_size,
    account_key_len, status_len`` followed by account key, status text and
    repeated ``uint16 length + payload`` records.
    """
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "unknown"
    timeout = 5.0
    try:
        try:
            timeout = max(0.2, min(30.0, float(getattr(args, "telebradesco_adapter_timeout", 5.0) or 5.0)))
        except (TypeError, ValueError) as exc:
            logging.warning(
                "%s TELE_ADAPTER_TIMEOUT_INVALID value=%r error=%s fallback=5.0",
                peer, getattr(args, "telebradesco_adapter_timeout", None), exc,
            )
        raw_header = await asyncio.wait_for(
            reader.readexactly(TELEBRADESCO_ADAPTER_REQUEST_HEADER.size),
            timeout=timeout,
        )
        magic, version, flags, login_len = TELEBRADESCO_ADAPTER_REQUEST_HEADER.unpack(raw_header)
        logging.info(
            "%s TELE_ADAPTER_RX_HEADER magic=%r version=%d flags=%d login_len=%d",
            peer, magic, version, flags, login_len,
        )
        logging.debug(
            "%s TELE_ADAPTER_RX_HEADER_WIRE bytes=%d hex=%s ascii=%s",
            peer, len(raw_header), raw_header.hex(" ").upper(),
            _telebradesco_adapter_ascii_preview(raw_header),
        )
        if magic != TELEBRADESCO_ADAPTER_REQUEST_MAGIC or version != TELEBRADESCO_ADAPTER_VERSION:
            _telebradesco_adapter_write_log(args, {
                "event": "request_rejected", "peer": peer, "reason": "bad_header",
                "magic": repr(magic), "version": version,
            })
            logging.warning("%s TELE_ADAPTER_BAD_HEADER magic=%r version=%s", peer, magic, version)
            await _telebradesco_adapter_send_error(writer, result_code=1, status="bad_header", peer=peer)
            return
        if flags != 0 or not (1 <= login_len <= TELEBRADESCO_ADAPTER_MAX_LOGIN):
            _telebradesco_adapter_write_log(args, {
                "event": "request_rejected", "peer": peer, "reason": "bad_request",
                "flags": flags, "login_length": login_len,
            })
            logging.warning("%s TELE_ADAPTER_BAD_LENGTH flags=%s login_len=%s", peer, flags, login_len)
            await _telebradesco_adapter_send_error(writer, result_code=1, status="bad_request", peer=peer)
            return
        login_raw = await asyncio.wait_for(reader.readexactly(login_len), timeout=timeout)
        try:
            login_text = login_raw.decode("ascii", "strict")
        except UnicodeDecodeError:
            _telebradesco_adapter_write_log(args, {
                "event": "request_rejected", "peer": peer,
                "reason": "non_ascii_login", "login_length": login_len,
            })
            await _telebradesco_adapter_send_error(writer, result_code=1, status="non_ascii_login", peer=peer)
            return

        masked_login = _telebradesco_adapter_mask_login(login_text)
        masked_login_raw = masked_login.encode("ascii", "replace")
        masked_request_wire = raw_header + masked_login_raw
        logging.info(
            "%s TELE_ADAPTER_RX_REQUEST login=%s login_len=%d bytes=%d",
            peer, masked_login, len(login_text), len(raw_header) + len(login_raw),
        )
        logging.debug(
            "%s TELE_ADAPTER_RX_REQUEST_WIRE masked=1 hex=%s ascii=%s",
            peer, masked_request_wire.hex(" ").upper(),
            _telebradesco_adapter_ascii_preview(masked_request_wire),
        )
        _telebradesco_adapter_write_log(args, {
            "event": "request", "peer": peer,
            "login": masked_login,
            "login_length": len(login_text),
        })

        accounts_file = pathlib.Path(getattr(args, "telebradesco_accounts_file", DEFAULT_TELEBRADESCO_ACCOUNTS_FILE))
        content_file = pathlib.Path(getattr(args, "telebradesco_content_file", DEFAULT_TELEBRADESCO_ACCOUNTS_CONTENT_FILE))
        try:
            accounts_data = read_telebradesco_accounts(
                accounts_file,
                accounts_content_file=content_file,
                create_if_missing=False,
            )
        except TelebradescoAccountsError as exc:
            _telebradesco_adapter_write_log(args, {
                "event": "error", "peer": peer, "reason": "accounts_unavailable",
                "error": str(exc),
            })
            logging.error("%s TELE_ADAPTER_ACCOUNTS_UNAVAILABLE error=%s", peer, exc)
            await _telebradesco_adapter_send_error(writer, result_code=2, status="accounts_unavailable", peer=peer)
            return

        auth_result = authenticate_telebradesco_login(accounts_data, login_text)
        status = str(auth_result.get("status") or "internal_error")
        credentials = auth_result.get("credentials") or {}
        account = auth_result.get("account")
        account_key = str(credentials.get("key") or "")

        if status == "ok" and isinstance(account, dict):
            compiled = compile_telebradesco_content(
                account.get("content", {}),
                data_cartao=credentials.get("data_cartao", ""),
            )
            try:
                access_result = record_telebradesco_account_access(
                    accounts_file,
                    account_key,
                    peer=peer,
                    login_text=login_text,
                    accounts_content_file=content_file,
                )
                if access_result:
                    logging.info(
                        "%s TELE_ADAPTER_ACCESS_RECORDED account=%s total_accesses=%s",
                        peer,
                        account_key,
                        access_result.get("total_accesses"),
                    )
            except Exception as exc:
                logging.warning("%s TELE_ADAPTER_ACCESS_RECORD_FAILED account=%s error=%s", peer, account_key, exc)
        else:
            status_code = str(auth_result.get("status_code") or "001")
            compiled = compile_telebradesco_status(
                status_code,
                None,
                block_mode=normalize_telebradesco_block_mode(
                    getattr(args, "telebradesco_block_mode", "native-020")
                ),
            )

        frames = [bytes(frame) for frame in compiled.get("frames", [])]
        if not frames or len(frames) > TELEBRADESCO_ADAPTER_MAX_FRAMES:
            raise ValueError(f"invalid compiled frame count: {len(frames)}")
        if any(not (1 <= len(frame) <= TELEBRADESCO_FRAME_PAYLOAD_MAX) for frame in frames):
            raise ValueError("compiled frame payload outside 1..121 bytes")
        if any(frame[-1] != 0x20 for frame in frames):
            raise ValueError("compiled frame payload missing trailing 0x20")

        calculated_stream_size = sum(len(frame) - 1 for frame in frames)
        stream_size = int(compiled.get("stream_size") or calculated_stream_size)
        if stream_size != calculated_stream_size:
            raise ValueError(
                f"compiled stream size mismatch: declared={stream_size} calculated={calculated_stream_size}"
            )
        account_raw = _tele_ascii(account_key)[:TELEBRADESCO_ADAPTER_MAX_TEXT].encode("ascii", "replace")
        status_raw = _tele_ascii(status)[:TELEBRADESCO_ADAPTER_MAX_TEXT].encode("ascii", "replace")
        header = TELEBRADESCO_ADAPTER_RESPONSE_HEADER.pack(
            TELEBRADESCO_ADAPTER_RESPONSE_MAGIC,
            TELEBRADESCO_ADAPTER_VERSION,
            0,
            len(frames),
            stream_size,
            len(account_raw),
            len(status_raw),
        )
        response_size = (
            len(header) + len(account_raw) + len(status_raw)
            + sum(2 + len(frame) for frame in frames)
        )
        logging.info(
            "%s TELE_ADAPTER_TX_HEADER magic=%r version=%d result=0 frames=%d "
            "stream=%d account_len=%d status_len=%d response_bytes=%d",
            peer, TELEBRADESCO_ADAPTER_RESPONSE_MAGIC, TELEBRADESCO_ADAPTER_VERSION,
            len(frames), stream_size, len(account_raw), len(status_raw), response_size,
        )
        logging.debug(
            "%s TELE_ADAPTER_TX_HEADER_WIRE hex=%s ascii=%s",
            peer, header.hex(" ").upper(), _telebradesco_adapter_ascii_preview(header),
        )
        logging.debug(
            "%s TELE_ADAPTER_TX_METADATA account=%s status=%s account_hex=%s status_hex=%s",
            peer, account_key or "-", status,
            account_raw.hex(" ").upper() or "-", status_raw.hex(" ").upper() or "-",
        )
        writer.write(header)
        writer.write(account_raw)
        writer.write(status_raw)
        for frame in frames:
            writer.write(struct.pack("!H", len(frame)))
            writer.write(frame)
        await writer.drain()
        frame_descriptors = [
            _telebradesco_adapter_wire_descriptor(frame, index)
            for index, frame in enumerate(frames)
        ]
        for descriptor in frame_descriptors:
            frame_index = int(descriptor["frame"])
            payload = frames[frame_index - 1]
            logging.debug(
                "%s TELE_ADAPTER_TX_FRAME frame=%d/%d sequence=%d payload_bytes=%d "
                "useful_bytes=%d checksum=%s ascii=%s payload_hex=%s wire_hex=%s",
                peer, frame_index, len(frames), descriptor["sequence"],
                descriptor["payload_length"], descriptor["useful_length"],
                descriptor["checksum"], _telebradesco_adapter_ascii_preview(payload),
                descriptor["payload_hex"], descriptor["wire_hex"],
            )
        logging.info(
            "%s TELE_ADAPTER_TX_COMPLETE frames=%d response_bytes=%d stream=%d",
            peer, len(frames), response_size, stream_size,
        )
        _telebradesco_adapter_write_log(args, {
            "event": "response",
            "peer": peer,
            "status": status,
            "account": account_key or "-",
            "selected_card": compiled.get("selected_data_cartao") or "-",
            "stream_size": stream_size,
            "frame_count": len(frames),
            "frames": frame_descriptors,
        })
        logging.info(
            "%s TELE_ADAPTER_RESPONSE status=%s account=%s frames=%d stream=%d selected_card=%s",
            peer,
            status,
            account_key or "-",
            len(frames),
            stream_size,
            compiled.get("selected_data_cartao") or "-",
        )
    except asyncio.IncompleteReadError as exc:
        _telebradesco_adapter_write_log(args, {
            "event": "error", "peer": peer, "reason": "truncated",
            "expected": exc.expected, "received": len(exc.partial),
        })
        logging.warning("%s TELE_ADAPTER_TRUNCATED expected=%s received=%s", peer, exc.expected, len(exc.partial))
    except asyncio.TimeoutError:
        _telebradesco_adapter_write_log(args, {
            "event": "error", "peer": peer, "reason": "timeout", "timeout": timeout,
        })
        logging.warning("%s TELE_ADAPTER_TIMEOUT seconds=%.3f", peer, timeout)
    except Exception as exc:
        _telebradesco_adapter_write_log(args, {
            "event": "error", "peer": peer, "reason": "internal_error",
            "error": repr(exc),
        })
        logging.exception("%s TELE_ADAPTER_INTERNAL_ERROR error=%s", peer, exc)
        try:
            await _telebradesco_adapter_send_error(writer, result_code=3, status="internal_error", peer=peer)
        except Exception:
            pass
    finally:
        await _close_client_writer(writer, peer, "Telebradesco adapter")
