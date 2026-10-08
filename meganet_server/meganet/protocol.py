"""MegaNet wire protocol primitives and token parsing.

Wire-format helpers for the validated MegaNet protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STX = 0x02

ETX = 0x03

EOT = 0x04

ACK = 0x06

NAK = 0x15

DLE = 0x10

CR = 0x0D

LF = 0x0A

def ascii_text(data: bytes) -> str:
    return data.decode("latin-1", errors="replace")

def display_payload(data: bytes) -> str:
    text = ascii_text(data)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text

def packet_text_bytes(text: str) -> bytes:
    return text.encode("latin-1", errors="replace")

def encode_server_frame(seq: int, payload: bytes) -> bytes:
    """Encode a server-to-ROM packet.

    The validated server format sends checksum high byte first and does not
    append ETX. This is intentionally different from the ROM-to-server frames
    parsed by ClientFrameParser, which use checksum low byte first plus ETX.
    """
    if not (0 <= seq <= 255):
        raise ValueError(f"seq out of range: {seq}")
    if len(payload) > 127:
        raise ValueError("payload too long for observed MegaNet packet format")
    length = len(payload)
    checksum = (length + seq + sum(payload)) & 0xFFFF
    return bytes([STX, length & 0x7F, seq]) + payload + bytes([
        (checksum >> 8) & 0xFF,
        checksum & 0xFF,
    ])

@dataclass
class DecodedFrame:
    seq: int
    payload: bytes
    checksum_expected: int
    checksum_actual: int
    valid: bool
    raw: bytes

class ClientFrameParser:
    """Incremental parser for client-to-server MegaNet frames."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[DecodedFrame]:
        self.buf.extend(data)
        frames: list[DecodedFrame] = []

        while True:
            # Discard noise before STX.
            while self.buf and self.buf[0] != STX:
                self.buf.pop(0)
            if len(self.buf) < 6:
                break

            length = self.buf[1] & 0x7F
            total = 1 + 1 + 1 + length + 2 + 1
            if len(self.buf) < total:
                break

            raw = bytes(self.buf[:total])
            del self.buf[:total]

            if raw[-1] != ETX:
                # Bad framing; attempt resync.
                continue

            seq = raw[2]
            payload = raw[3:3 + length]
            checksum_expected = raw[3 + length] | (raw[4 + length] << 8)
            checksum_actual = (length + seq + sum(payload)) & 0xFFFF
            frames.append(DecodedFrame(
                seq=seq,
                payload=payload,
                checksum_expected=checksum_expected,
                checksum_actual=checksum_actual,
                valid=(checksum_expected == checksum_actual),
                raw=raw,
            ))
        return frames

def parse_meganet_id_payload(payload: bytes) -> Optional[tuple[str, str, str]]:
    """Parse the ROM identification packet: #id <user> <password> [service]#.

    Observed example from the ROM after #bemvindo#/#ready#:
        #id kavera 123ABC SSO#\r
    """
    text = ascii_text(payload.rstrip(b"\r"))
    if not text.lower().startswith("#id ") or not text.endswith("#"):
        return None
    body = text[4:-1].strip()
    parts = body.split()
    if len(parts) < 2:
        return None
    user = parts[0].strip()
    password = parts[1].strip()
    service = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
    if not user or not password:
        return None
    return user, password, service

def parse_password_change_payload(payload: bytes) -> Optional[tuple[str, str, str]]:
    """Parse the ROM password-change packet.

    Observed behavior:
        #senha kavera 123ABC COCO12#\r

    Format:
        #senha <nome_meganet> <senha_antiga> <senha_nova>#
    """
    text = ascii_text(payload.rstrip(b"\r"))
    if not text.lower().startswith("#senha ") or not text.endswith("#"):
        return None
    body = text[len("#senha "):-1].strip()
    parts = body.split()
    if len(parts) < 3:
        return None
    nome_meganet = parts[0].strip()
    old_password = parts[1].strip()
    new_password = parts[2].strip()
    if not nome_meganet or not old_password or not new_password:
        return None
    return nome_meganet, old_password, new_password

