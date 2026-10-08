from __future__ import annotations

import asyncio
import json
import threading

import pytest

from meganet_server import common, storage
from meganet_server.telebradesco import protocol


LOGIN_TEXT = b"MEGABRA292400234567123412345678"


def _bitrev_wire(logical: bytes) -> bytes:
    return bytes(protocol._tele_reverse_bits8(value) for value in logical)


def _stuff_after_initial_dle(logical: bytes) -> bytes:
    assert logical.startswith(b"\x10")
    wire = bytearray([0x10])
    for value in logical[1:]:
        wire.append(value)
        if value == 0x10:
            wire.append(value)
    return bytes(wire)


def _synthetic_login_wire(
    payload: bytes = LOGIN_TEXT,
    *,
    tag: int = 0x80,
    seq: int = 2,
) -> bytes:
    prefix = bytes([0x10, tag & 0xFF, seq & 0xFF, len(payload) & 0xFF]) + payload
    checksum = sum(prefix) & 0xFFFF
    encoded = _stuff_after_initial_dle(prefix + checksum.to_bytes(2, "big"))
    return _bitrev_wire(encoded + b"\x10\x03")


def test_tele_reverse_bits8_is_involution_for_all_bytes():
    for value in range(256):
        assert protocol._tele_reverse_bits8(protocol._tele_reverse_bits8(value)) == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", False),
        (b"\xC0", False),
        (b"\x08\xC0", True),
        (b"\x08\x08\xC0", False),
        (b"\x08\x08\x08\xC0", True),
        (b"\x08\x00", False),
    ],
)
def test_tele_login_wire_terminated_respects_dle_parity(raw, expected):
    assert protocol._tele_login_wire_terminated(raw) is expected


def test_tele_unstuff_dle_bytes_rejects_unpaired_dle():
    assert protocol._tele_unstuff_dle_bytes(b"A\x10\x10B") == b"A\x10B"
    with pytest.raises(ValueError, match="stuffing"):
        protocol._tele_unstuff_dle_bytes(b"A\x10B")
    with pytest.raises(ValueError, match="stuffing"):
        protocol._tele_unstuff_dle_bytes(b"A\x10")


def test_tele_login_confirmation_waits_for_complete_frame_and_preserves_remainder():
    logical_frame = bytes.fromhex("FF C8 C1 00 52 13 01 01 00")
    assert protocol._tele_extract_login_confirmation(b"") is None
    assert protocol._tele_extract_login_confirmation(_bitrev_wire(b"noise")) is None
    assert protocol._tele_extract_login_confirmation(_bitrev_wire(logical_frame[:-1])) is None

    logical = b"\x00\x00\x10\x41" + logical_frame + b"AT"
    parsed = protocol._tele_extract_login_confirmation(_bitrev_wire(logical))
    assert parsed is not None
    assert parsed["logical_prefix"] == b"\x00\x00\x10\x41"
    assert parsed["logical_frame"] == logical_frame
    assert parsed["logical_remainder"] == b"AT"


def test_tele_t30_header_accepts_only_login_fcf_and_final_bit_variant():
    assert protocol._is_telebradesco_t30_login_header(b"\xFF\xC0\xC2")
    assert protocol._is_telebradesco_t30_login_header(b"\xFF\xC8\xC2")
    assert not protocol._is_telebradesco_t30_login_header(b"\xFF\xC8\xC1")
    assert not protocol._is_telebradesco_t30_login_header(b"\xFF\xC0")
    assert not protocol._is_telebradesco_t30_login_header(b"\xFE\xC0\xC2")


def test_tele_t30_decoder_defensive_errors():
    with pytest.raises(ValueError, match="incompleto"):
        protocol._decode_telebradesco_t30_login(b"\xFF\xC0\xC2")
    with pytest.raises(ValueError, match="Cabeçalho"):
        protocol._decode_telebradesco_t30_login(b"\xFF\xC0\xC1X")
    with pytest.raises(ValueError, match="ASCII"):
        protocol._decode_telebradesco_t30_login(b"\xFF\xC0\xC2\xFF")
    with pytest.raises(ValueError, match="MEGABRA"):
        protocol._decode_telebradesco_t30_login(b"\xFF\xC0\xC2INVALID")


def test_tele_synthetic_login_legacy_decoder_roundtrip_and_dle_stuffing():
    payload = LOGIN_TEXT[:-1] + b"0"  # remains valid and includes ordinary data path
    decoded = protocol.decode_telebradesco_login_frame(_synthetic_login_wire(payload, tag=0x10, seq=0x10))
    assert decoded["frame_format"] == "length_checksum"
    assert decoded["tag"] == 0x10
    assert decoded["sequence"] == 0x10
    assert decoded["payload"] == payload
    assert decoded["login_text"] == payload.decode("ascii")


def test_tele_synthetic_login_decoder_defensive_errors():
    with pytest.raises(ValueError, match="Cabeçalho"):
        protocol._decode_telebradesco_length_checksum_login(b"")
    with pytest.raises(ValueError, match="incompleto"):
        protocol._decode_telebradesco_length_checksum_login(b"\x10\x80\x02")

    good_raw = _synthetic_login_wire()
    decoded_wire = bytes(protocol._tele_reverse_bits8(value) for value in good_raw)
    encoded = decoded_wire[:-2]
    logical = bytearray([encoded[0]])
    logical.extend(protocol._tele_unstuff_dle_bytes(encoded[1:]))

    wrong_length = bytearray(logical)
    wrong_length[3] += 1
    with pytest.raises(ValueError, match="Tamanho"):
        protocol._decode_telebradesco_length_checksum_login(_stuff_after_initial_dle(bytes(wrong_length)))

    bad_checksum = bytearray(logical)
    bad_checksum[-1] ^= 0x01
    with pytest.raises(ValueError, match="Checksum"):
        protocol._decode_telebradesco_length_checksum_login(_stuff_after_initial_dle(bytes(bad_checksum)))

    non_ascii = b"MEGABRA29240023456712341234567\xFF"
    prefix = bytes([0x10, 0x80, 2, len(non_ascii)]) + non_ascii
    checksum = sum(prefix) & 0xFFFF
    with pytest.raises(ValueError, match="ASCII"):
        protocol._decode_telebradesco_length_checksum_login(
            _stuff_after_initial_dle(prefix + checksum.to_bytes(2, "big"))
        )

    invalid_payload = b"NOT-MEGABRA"
    prefix = bytes([0x10, 0x80, 2, len(invalid_payload)]) + invalid_payload
    checksum = sum(prefix) & 0xFFFF
    with pytest.raises(ValueError, match="MEGABRA"):
        protocol._decode_telebradesco_length_checksum_login(
            _stuff_after_initial_dle(prefix + checksum.to_bytes(2, "big"))
        )


def test_tele_login_frame_top_level_defensive_errors():
    with pytest.raises(ValueError, match="vazio"):
        protocol.decode_telebradesco_login_frame(b"")
    with pytest.raises(ValueError, match="Terminador"):
        protocol.decode_telebradesco_login_frame(_bitrev_wire(b"\xFF\xC0\xC2" + LOGIN_TEXT))
    with pytest.raises(ValueError, match="desconhecido"):
        protocol.decode_telebradesco_login_frame(_bitrev_wire(b"XYZ\x10\x03"))


def test_tele_class1_wire_bounds_and_masks_seq_tag():
    payload = b"X" * protocol.TELEBRADESCO_FRAME_PAYLOAD_MAX
    wire = protocol._tele_class1_wire(payload, seq=0x102, tag=0x180)
    encoded = wire[:-2]
    logical = bytes([encoded[0]]) + protocol._tele_unstuff_dle_bytes(encoded[1:])
    assert logical[1] == 0x80
    assert logical[2] == 0x02
    assert logical[3] == len(payload)

    with pytest.raises(ValueError, match="excede"):
        protocol._tele_class1_wire(payload + b"X", seq=0)


def test_atomic_write_text_is_safe_for_concurrent_writers(tmp_path, monkeypatch):
    path = tmp_path / "state.txt"
    barrier = threading.Barrier(2)
    original_replace = storage.os.replace

    def synchronized_replace(src, dst):
        barrier.wait(timeout=5)
        return original_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", synchronized_replace)
    errors: list[BaseException] = []

    def write(value: str) -> None:
        try:
            storage.atomic_write_text(path, value)
        except BaseException as exc:  # assertion captures the concurrency failure itself
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(value,)) for value in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert path.read_text(encoding="utf-8") in {"alpha", "beta"}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_text_cleans_temp_and_preserves_destination_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.txt"
    path.write_text("before", encoding="utf-8")

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        storage.atomic_write_text(path, "after")

    assert path.read_text(encoding="utf-8") == "before"
    assert not list(tmp_path.glob("*.tmp"))


def test_read_json_dict_handles_invalid_data_and_logs(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert storage.read_json_dict(path, default={"ok": True}, log_prefix="STATE") == {"ok": True}
    assert "STATE_READ_FAILED" in caplog.text

    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert storage.read_json_dict(path, default={"ok": True}) == {"ok": True}


def test_append_jsonl_fsync_error_does_not_lose_record(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"

    def fail_fsync(_fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(storage.os, "fsync", fail_fsync)
    storage.append_jsonl(path, {"event": "ok"}, fsync=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"event": "ok"}


class _Writer:
    def __init__(self, *, close_error=None, wait_error=None):
        self.close_error = close_error
        self.wait_error = wait_error
        self.closed = False
        self.waited = False

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise self.close_error

    async def wait_closed(self) -> None:
        self.waited = True
        if self.wait_error:
            raise self.wait_error

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default=None):
        return default


def test_close_client_writer_contains_transport_close_errors():
    async def run():
        normal = _Writer()
        assert isinstance(normal, common.AsyncByteWriter)
        await common._close_client_writer(normal, "peer", "svc")
        assert normal.closed and normal.waited

        close_failure = _Writer(close_error=ConnectionResetError("reset"))
        await common._close_client_writer(close_failure, "peer", "svc")
        assert close_failure.closed and not close_failure.waited

        wait_failure = _Writer(wait_error=BrokenPipeError("pipe"))
        await common._close_client_writer(wait_failure, "peer", "svc")
        assert wait_failure.closed and wait_failure.waited

    asyncio.run(run())


def test_format_datetime_fallbacks_and_utc_now_shape():
    assert common.format_datetime(None) == "-"
    assert common.format_datetime("2026-10-04T17:55:12Z") == "04/10/2026 17:55"
    assert common.format_datetime("2026-10-04 17:55 trailing") == "04/10/2026 17:55"
    assert common.format_datetime("not-a-date-longer-than-sixteen") == "not-a-date-longe"
    assert common.utc_now_iso().endswith("+00:00")
