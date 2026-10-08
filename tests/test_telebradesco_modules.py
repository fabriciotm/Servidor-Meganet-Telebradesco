import argparse
import asyncio
import hashlib

from meganet_server import server
from meganet_server.telebradesco import accounts, adapter, content, protocol, session
from meganet_server.telebradesco.states import TelebradescoMode, TelebradescoStage


def test_server_compatibility_reexports_telebradesco_modules():
    assert server.compile_telebradesco_content is content.compile_telebradesco_content
    assert server.read_telebradesco_accounts is accounts.read_telebradesco_accounts
    assert server.decode_telebradesco_login_frame is protocol.decode_telebradesco_login_frame
    assert server.handle_telebradesco_adapter_client is adapter.handle_telebradesco_adapter_client
    assert server.TelebradescoSession is session.TelebradescoSession


def test_telebradesco_default_stream_golden_vector():
    compiled = content.compile_telebradesco_content(content.default_telebradesco_content())
    assert len(compiled["stream"]) == 443
    assert [len(frame) for frame in compiled["frames"]] == [121, 121, 121, 84]
    assert hashlib.sha256(compiled["stream"]).hexdigest() == "541b02650da08d95c6be385e1091c786e34631f34fc15c0a039c220fddd0160d"


def test_telebradesco_status_golden_vectors():
    expected = {
        "999": (150, "ffb80c5c1f94368f24ec7d355878d7cbce8f2aebd32250e82e046c26717c0b4a"),
        "020": (26, "9f0f67b052aa8d536aa839531201b4670086e27079638c4784fed9bfdf212d80"),
        "040": (26, "e5fca7b33382739885334f51b368aee91270437a6dfcbee810d68093f2ee7f53"),
        "042": (26, "97df05bc0f35c89eb8f13bd5638242c1b62bf7952ecf6998648478c562e729a2"),
        "011": (150, "f1fe98445b3f8217eb5aad1a30af84d5662216bdeaf09771449149b22c93a5b5"),
    }
    for code, (size, digest) in expected.items():
        compiled = content.compile_telebradesco_status(code, None, block_mode="native-020")
        assert len(compiled["stream"]) == size
        assert hashlib.sha256(compiled["stream"]).hexdigest() == digest


class _FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("127.0.0.1", 12345)
        return default

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)


def test_telebradesco_adapter_rejects_invalid_magic(tmp_path):
    async def run():
        reader = asyncio.StreamReader()
        bad = adapter.TELEBRADESCO_ADAPTER_REQUEST_HEADER.pack(b"BAD!", 1, 0, 1) + b"X"
        reader.feed_data(bad)
        reader.feed_eof()
        writer = _FakeWriter()
        args = argparse.Namespace(
            telebradesco_adapter_timeout=1.0,
            telebradesco_adapter_log_file=tmp_path / "adapter.jsonl",
        )
        await adapter.handle_telebradesco_adapter_client(reader, writer, args)
        raw = bytes(writer.data)
        assert raw.startswith(adapter.TELEBRADESCO_ADAPTER_RESPONSE_MAGIC)
        magic, version, result, frame_count, stream_size, account_len, status_len = adapter.TELEBRADESCO_ADAPTER_RESPONSE_HEADER.unpack(
            raw[: adapter.TELEBRADESCO_ADAPTER_RESPONSE_HEADER.size]
        )
        assert magic == b"TBR1"
        assert version == 1
        assert result == 1
        assert frame_count == 0
        assert stream_size == 0
        assert account_len == 0
        status = raw[adapter.TELEBRADESCO_ADAPTER_RESPONSE_HEADER.size:][:status_len].decode("ascii")
        assert status == "bad_header"
        assert writer.closed

    asyncio.run(run())


def test_telebradesco_typed_initial_state(tmp_path):
    writer = _FakeWriter()
    sess = session.TelebradescoSession(
        writer,
        peer="typed-state-test",
        accounts_file=tmp_path / "accounts.json",
        accounts_content_file=tmp_path / "accounts_content.json",
    )
    assert sess.mode is TelebradescoMode.COMMAND
    assert sess.stage is TelebradescoStage.AWAIT_INITIAL_FRH
    assert sess.mode == "command"
    assert sess.stage == "await_initial_frh"
