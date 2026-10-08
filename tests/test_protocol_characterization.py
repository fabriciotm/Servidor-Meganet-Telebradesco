from __future__ import annotations

import asyncio
import pathlib
import struct

import pytest

from meganet_server import server as s


def make_client_frame(seq: int, payload: bytes) -> bytes:
    checksum = (len(payload) + seq + sum(payload)) & 0xFFFF
    return bytes([s.STX, len(payload), seq]) + payload + bytes([checksum & 0xFF, checksum >> 8, s.ETX])


def test_meganet_server_frame_direction_and_checksum():
    payload = b"#ready#\r"
    frame = s.encode_server_frame(0x12, payload)
    checksum = (len(payload) + 0x12 + sum(payload)) & 0xFFFF
    assert frame == bytes([0x02, len(payload), 0x12]) + payload + checksum.to_bytes(2, "big")
    assert frame[-1] != s.ETX


def test_meganet_client_frame_is_low_high_plus_etx_and_incremental():
    payload = b"#bye#\r"
    raw = make_client_frame(7, payload)
    parser = s.ClientFrameParser()
    assert parser.feed(raw[:4]) == []
    frames = parser.feed(raw[4:])
    assert len(frames) == 1
    assert frames[0].seq == 7
    assert frames[0].payload == payload
    assert frames[0].valid is True
    assert raw[-1] == s.ETX


def test_meganet_id_and_password_change_tokens():
    assert s.parse_meganet_id_payload(b"#id kavera 123ABC SSO#\r") == ("kavera", "123ABC", "SSO")
    assert s.parse_password_change_payload(b"#senha kavera ABC123 NOVA12#\r") == ("kavera", "ABC123", "NOVA12")


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"TBA1", "telebradesco-adapter"),
        (b"MGA1", "meganet-auto-probe"),
        (b"ATX3DT,,777777\r", "telebradesco"),
        (b"ATX3DP,123456\r", "meganet"),
        (b"#send news 3#\r", "meganet"),
        (b"A", None),
    ],
)
def test_unified_listener_detection(raw, expected):
    assert s._unified_tcp_detect_service(raw) == expected


def test_telebradesco_megabra_layout_and_card_selector():
    login = "MEGABRA292400234567123412345678"
    parsed = s.parse_telebradesco_login_text(login)
    assert parsed is not None
    assert parsed["agencia"] == "2924"
    assert parsed["conta"] == "0023456"
    assert parsed["digito"] == "7"
    assert parsed["senha"] == "1234"
    assert parsed["restante"] == "12345678"
    assert parsed["restante_prefixo"] == "1234"
    assert parsed["data_cartao"] == "5678"


def test_telebradesco_real_t30_login_decoder():
    login = b"MEGABRA292400234567123412345678"
    logical = b"\xFF\xC0\xC2" + login + b"\x10\x03"
    raw = bytes(s._tele_reverse_bits8(x) for x in logical)
    decoded = s.decode_telebradesco_login_frame(raw)
    assert decoded["frame_format"] == "t30_hdlc"
    assert decoded["header"] == b"\xFF\xC0\xC2"
    assert decoded["login_text"] == login.decode("ascii")


def test_telebradesco_login_confirmation_accepts_prefix():
    logical = b"\x00\x00\x10\x41" + bytes.fromhex("FF C8 C1 00 52 13 01 01 00") + b"AT"
    raw = bytes(s._tele_reverse_bits8(x) for x in logical)
    parsed = s._tele_extract_login_confirmation(raw)
    assert parsed is not None
    assert parsed["logical_prefix"] == b"\x00\x00\x10\x41"
    assert parsed["logical_frame"] == bytes.fromhex("FF C8 C1 00 52 13 01 01 00")
    assert parsed["logical_remainder"] == b"AT"


def test_telebradesco_rle_spaces_and_zeroes():
    assert s._tele_compress(b"     ") == bytes([0x95])
    assert s._tele_compress(b"0000") == bytes([0x84])
    assert s._tele_compress(b" 0 ") == b" 0 "


@pytest.mark.parametrize(
    "code, mode, expected",
    [
        ("999", "native-020", ("999", "999", "999")),
        ("020", "native-020", ("020", "020", "999")),
        ("040", "native-020", ("040", "040", "040")),
        ("042", "native-020", ("042", "042", "042")),
    ],
)
def test_telebradesco_status_slots(code, mode, expected):
    assert s._tele_auth_status_slots(code, mode) == expected


def test_telebradesco_content_frames_preserve_protocol_atoms():
    compiled = s.compile_telebradesco_content(s.default_telebradesco_content())
    assert compiled["status_slots"] == ("999", "999", "999")
    assert len(compiled["frames"]) >= 1
    assert all(1 <= len(frame) <= 121 for frame in compiled["frames"])
    assert all(frame.endswith(b" ") for frame in compiled["frames"])
    stream = compiled["stream"]
    trailer = s._tele_marker("0") + s._tele_control_record("999") * 3
    assert stream.endswith(trailer)
    # Reconstruct useful bytes; frame padding is one trailing space per frame.
    assert b"".join(frame[:-1] for frame in compiled["frames"]) == stream


def test_telebradesco_class1_wire_checksum_and_dle_stuffing():
    payload = b"A\x10B "
    seq = 2
    wire = s._tele_class1_wire(payload, seq)
    assert wire.endswith(b"3\r")
    encoded = wire[:-2]
    assert encoded[0] == 0x10
    # Payload DLE must be doubled in the encoded wire.
    assert b"\x10\x10" in encoded[1:]
    # Unstuff and validate the logical checksum.
    logical = bytes([encoded[0]]) + s._tele_unstuff_dle_bytes(encoded[1:])
    prefix = logical[:-2]
    assert logical[-2:] == (sum(prefix) & 0xFFFF).to_bytes(2, "big")
    assert prefix[:4] == bytes([0x10, 0x80, seq, len(payload)])
    assert prefix[4:] == payload


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True
    def is_closing(self): return self.closed


def test_telebradesco_session_is_silent_until_first_command(tmp_path):
    async def run():
        writer = FakeWriter()
        sess = s.TelebradescoSession(
            writer, peer="test",
            accounts_file=tmp_path / "accounts.json",
            accounts_content_file=tmp_path / "accounts_content.json",
        )
        await sess.start()
        assert bytes(writer.data) == b""
        await sess.feed(b"AT+FRH=3\r")
        # Command echo followed by the Class-1 response used by the server.
        assert bytes(writer.data).startswith(b"AT+FRH=3\r")
        assert bytes(writer.data).endswith(b"1\r\x10\x03" + b"0\r")
        assert sess.stage == "await_login_fth"
    asyncio.run(run())
