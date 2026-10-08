from __future__ import annotations

from typing import Optional

from .accounts import parse_telebradesco_login_text
from .content import TELEBRADESCO_FRAME_PAYLOAD_MAX

def _tele_reverse_bits8(value: int) -> int:
    value &= 0xFF
    value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
    value = ((value & 0xCC) >> 2) | ((value & 0x33) << 2)
    value = ((value & 0xAA) >> 1) | ((value & 0x55) << 1)
    return value
TELEBRADESCO_LOGIN_FRAME_MAX = 256
def _tele_login_wire_terminated(raw: bytes | bytearray) -> bool:
    """Return True only for an unescaped bit-reversed DLE/ETX terminator."""
    if not raw or raw[-1] != 0xC0:
        return False
    count = 0
    index = len(raw) - 2
    while index >= 0 and raw[index] == 0x08:
        count += 1
        index -= 1
    return bool(count % 2)
def _tele_unstuff_dle_bytes(encoded: bytes) -> bytes:
    """Undo DLE doubling used by the Class-1 DTE stream."""
    logical = bytearray()
    index = 0
    while index < len(encoded):
        value = encoded[index]
        if value == 0x10:
            if index + 1 >= len(encoded) or encoded[index + 1] != 0x10:
                raise ValueError("DLE stuffing inválido no quadro de login.")
            logical.append(0x10)
            index += 2
        else:
            logical.append(value)
            index += 1
    return bytes(logical)
TELEBRADESCO_LOGIN_T30_ADDRESS = 0xFF
TELEBRADESCO_LOGIN_T30_CONTROL_BASE = 0xC0
TELEBRADESCO_LOGIN_T30_CONTROL_FINAL_BIT = 0x08
TELEBRADESCO_LOGIN_T30_FCF = 0xC2
TELEBRADESCO_LOGIN_T30_HEADER_SIZE = 3
TELEBRADESCO_LOGIN_CONFIRM_HEADER = bytes([0xFF, 0xC8, 0xC1])
TELEBRADESCO_LOGIN_CONFIRM_SIZE = 9
TELEBRADESCO_LOGIN_CONFIRM_MAX_RAW = 64
def _tele_extract_login_confirmation(raw: bytes) -> Optional[dict]:
    """Extract the complete nine-byte login confirmation from modem bytes.

    The bytes received from the modem are bit-reversed.  Captures may include
    a logical four-byte prefix ``00 00 10 41`` before the actual confirmation:

        FF C8 C1 00 52 13 01 01 00

    Earlier versions switched back to command mode after nine *physical* bytes.
    With the prefix present, only the first five confirmation bytes had been
    consumed and the final ``13 01 01 00`` were echoed as if they were an AT
    command.  Return ``None`` until the complete confirmation is available.
    """
    raw_bytes = bytes(raw)
    if not raw_bytes:
        return None
    logical = bytes(_tele_reverse_bits8(value) for value in raw_bytes)
    start = logical.find(TELEBRADESCO_LOGIN_CONFIRM_HEADER)
    if start < 0:
        return None
    end = start + TELEBRADESCO_LOGIN_CONFIRM_SIZE
    if len(logical) < end:
        return None
    return {
        "raw_consumed": raw_bytes[:end],
        "raw_remainder": raw_bytes[end:],
        "logical_prefix": logical[:start],
        "logical_frame": logical[start:end],
        "logical_remainder": logical[end:],
    }
def _is_telebradesco_t30_login_header(header: bytes) -> bool:
    """Return whether *header* belongs to the observed Telebradesco login frame.

    Captures use ``FF C0 C2``.  The HDLC control byte may carry the final-bit
    variant ``C8`` while the address and frame-type byte remain unchanged.
    The later nine-byte login confirmation is ``FF C8 C1`` and must not be
    mistaken for the credential frame.
    """
    if len(header) != TELEBRADESCO_LOGIN_T30_HEADER_SIZE:
        return False
    address, control, frame_type = header
    control_without_final = control & ~TELEBRADESCO_LOGIN_T30_CONTROL_FINAL_BIT
    return (
        address == TELEBRADESCO_LOGIN_T30_ADDRESS
        and control_without_final == TELEBRADESCO_LOGIN_T30_CONTROL_BASE
        and frame_type == TELEBRADESCO_LOGIN_T30_FCF
    )
def _decode_telebradesco_t30_login(encoded_frame: bytes) -> dict:
    """Decode the real credential frame emitted under ``AT+FTH=3``.

    After per-byte bit reversal, the observed credential frame is:

        FF C0 C2 + MEGABRA... + DLE ETX

    ``FF C8 C2`` is accepted as the same frame with the control final bit set.
    The modem, not the application, is responsible for the HDLC FCS.
    """
    logical = _tele_unstuff_dle_bytes(encoded_frame)
    header_size = TELEBRADESCO_LOGIN_T30_HEADER_SIZE
    if len(logical) <= header_size:
        raise ValueError("Quadro HDLC/T.30 de login incompleto.")
    header = logical[:header_size]
    if not _is_telebradesco_t30_login_header(header):
        raise ValueError(
            "Cabeçalho HDLC/T.30 de login inválido: " + header.hex(" ").upper() + "."
        )
    payload = logical[header_size:]
    try:
        login_text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Payload de login não é ASCII válido.") from exc
    credentials = parse_telebradesco_login_text(login_text)
    if not credentials:
        raise ValueError("Payload MEGABRA não corresponde ao formato observado.")
    return {
        "frame_format": "t30_hdlc",
        "header": header,
        "payload": payload,
        "login_text": login_text,
        "credentials": credentials,
    }
def _decode_telebradesco_length_checksum_login(encoded_frame: bytes) -> dict:
    """Decode the legacy synthetic length/checksum format accepted for compatibility.

    This path remains for compatibility with captures and tests produced by
    those versions.  The real cartridge uses the HDLC/T.30 path above.
    """
    if not encoded_frame or encoded_frame[0] != 0x10:
        raise ValueError("Cabeçalho Class-1 sintético inválido.")

    logical = bytearray([0x10])
    logical.extend(_tele_unstuff_dle_bytes(encoded_frame[1:]))
    if len(logical) < 6:
        raise ValueError("Quadro Class-1 sintético incompleto.")
    payload_length = logical[3]
    expected_size = 4 + payload_length + 2
    if len(logical) != expected_size:
        raise ValueError(
            f"Tamanho Class-1 divergente: declarado={payload_length}, recebido={len(logical) - 6}."
        )
    expected_checksum = (logical[-2] << 8) | logical[-1]
    calculated_checksum = sum(logical[:-2]) & 0xFFFF
    if expected_checksum != calculated_checksum:
        raise ValueError(
            f"Checksum Class-1 inválido: recebido={expected_checksum:04X}, calculado={calculated_checksum:04X}."
        )

    payload = bytes(logical[4:4 + payload_length])
    try:
        login_text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Payload de login não é ASCII válido.") from exc
    credentials = parse_telebradesco_login_text(login_text)
    if not credentials:
        raise ValueError("Payload MEGABRA não corresponde ao formato observado.")
    return {
        "frame_format": "length_checksum",
        "header": bytes(logical[:4]),
        "tag": logical[1],
        "sequence": logical[2],
        "payload": payload,
        "login_text": login_text,
        "credentials": credentials,
        "checksum": expected_checksum,
    }
def decode_telebradesco_login_frame(raw: bytes) -> dict:
    """Decode and validate a bit-reversed Telebradesco login frame."""
    if not raw:
        raise ValueError("Quadro de login vazio.")
    decoded_wire = bytes(_tele_reverse_bits8(value) for value in raw)
    if not decoded_wire.endswith(b"\x10\x03"):
        raise ValueError("Terminador DLE/ETX ausente.")

    encoded_frame = decoded_wire[:-2]
    if len(encoded_frame) >= TELEBRADESCO_LOGIN_T30_HEADER_SIZE:
        header = encoded_frame[:TELEBRADESCO_LOGIN_T30_HEADER_SIZE]
        if _is_telebradesco_t30_login_header(header):
            return _decode_telebradesco_t30_login(encoded_frame)
    if encoded_frame.startswith(b"\x10"):
        return _decode_telebradesco_length_checksum_login(encoded_frame)

    header_text = encoded_frame[:3].hex(" ").upper() if encoded_frame else "<vazio>"
    raise ValueError(f"Cabeçalho de login desconhecido: {header_text}.")
def _tele_class1_wire(payload: bytes, seq: int, tag: int = 0x80) -> bytes:
    if len(payload) > TELEBRADESCO_FRAME_PAYLOAD_MAX:
        raise ValueError(f"Payload Class-1 excede 121 bytes: {len(payload)}")
    frame = bytearray([0x10, tag & 0xFF, seq & 0xFF, len(payload) & 0xFF])
    frame.extend(payload)
    checksum = sum(frame) & 0xFFFF
    frame.extend([(checksum >> 8) & 0xFF, checksum & 0xFF])
    wire = bytearray([frame[0]])
    for value in frame[1:]:
        wire.append(value)
        if value == 0x10:
            wire.append(value)
    wire.extend(b"3\r")
    return bytes(wire)
