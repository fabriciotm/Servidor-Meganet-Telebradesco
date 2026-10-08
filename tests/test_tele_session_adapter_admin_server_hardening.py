from __future__ import annotations

import argparse
import asyncio
import http.client
import re
import urllib.parse


from meganet_server.admin import server as admin_server
from meganet_server.telebradesco import adapter
from meganet_server.telebradesco import session as tele_session


class _CloseFailWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.close_calls = 0

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.close_calls += 1
        raise OSError("transport already gone")

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _AdapterWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 41000)
        return default

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def test_telebradesco_delayed_close_tolerates_transport_close_error(tmp_path):
    async def run() -> None:
        writer = _CloseFailWriter()
        sess = tele_session.TelebradescoSession(
            writer,
            peer="close-failure",
            accounts_file=tmp_path / "accounts.json",
            accounts_content_file=tmp_path / "accounts_content.json",
            error_hold_seconds=0,
        )
        await sess._close_later(0)
        assert writer.close_calls == 1

    asyncio.run(run())


def test_adapter_invalid_timeout_falls_back_and_always_closes_writer(tmp_path):
    async def run() -> None:
        reader = asyncio.StreamReader()
        request = adapter.TELEBRADESCO_ADAPTER_REQUEST_HEADER.pack(b"BAD!", 1, 0, 1) + b"X"
        reader.feed_data(request)
        reader.feed_eof()
        writer = _AdapterWriter()
        args = argparse.Namespace(
            telebradesco_adapter_timeout="not-a-number",
            telebradesco_adapter_log_file=tmp_path / "adapter.jsonl",
        )
        await adapter.handle_telebradesco_adapter_client(reader, writer, args)
        assert writer.closed is True
        assert bytes(writer.data).startswith(adapter.TELEBRADESCO_ADAPTER_RESPONSE_MAGIC)

    asyncio.run(run())


def test_start_admin_server_context_creation_failure_is_nonfatal(tmp_path, monkeypatch):
    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )

    def fail_create(_args):
        raise OSError("admin users storage unavailable")

    monkeypatch.setattr(admin_server.AdminApplicationContext, "create", fail_create)
    assert admin_server.start_admin_server(args) is None


def test_admin_login_persistence_failure_returns_500_without_orphan_session(tmp_path):
    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/login")
        response = conn.getresponse()
        login_html = response.read()
        assert response.status == 200
        match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert match
        csrf = match.group(1).decode("utf-8")

        manager = httpd.RequestHandlerClass.app_context.auth_manager

        def fail_write(_data):
            raise OSError("disk full")

        manager.write_users = fail_write
        form = urllib.parse.urlencode({
            "_csrf": csrf,
            "username": "admin",
            "password": "admin",
            "next": "/",
        })
        conn.request(
            "POST",
            "/login",
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        response = conn.getresponse()
        payload = response.read()
        assert response.status == 500
        assert response.getheader("Set-Cookie") is None
        assert b"persist" in payload.lower() or b"salv" in payload.lower()
        assert manager.sessions == {}
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_admin_logout_requires_csrf_and_preserves_session_on_rejection(tmp_path):
    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        manager = httpd.RequestHandlerClass.app_context.auth_manager
        token = manager.create_session("admin")
        cookie = f"SMT_ADMIN_SESSION={urllib.parse.quote(token)}"
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        body = urllib.parse.urlencode({})
        conn.request(
            "POST",
            "/logout",
            body=body,
            headers={
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        response.read()
        assert response.status == 403
        assert token in manager.sessions
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_adapter_truncated_header_and_log_failure_still_cleanup(tmp_path, monkeypatch):
    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"TBA")
        reader.feed_eof()
        writer = _AdapterWriter()
        args = argparse.Namespace(
            telebradesco_adapter_timeout=0.2,
            telebradesco_adapter_log_file=tmp_path / "adapter.jsonl",
        )

        def fail_log(*_args, **_kwargs):
            raise OSError("log disk unavailable")

        monkeypatch.setattr(adapter, "append_jsonl", fail_log)
        await adapter.handle_telebradesco_adapter_client(reader, writer, args)
        assert writer.closed is True
        assert bytes(writer.data) == b""

    asyncio.run(run())


def test_telebradesco_unknown_ack_keeps_current_frame_for_retry(tmp_path):
    async def run() -> None:
        writer = _AdapterWriter()
        sess = tele_session.TelebradescoSession(
            writer,
            peer="unknown-ack",
            accounts_file=tmp_path / "accounts.json",
            accounts_content_file=tmp_path / "accounts_content.json",
            error_hold_seconds=0,
        )
        sess.mode = tele_session.TelebradescoMode.ACK3
        sess.ack_context = "data"
        sess.frame_index = 0
        sess.sequence = 2
        # Logical final byte 0x7f, bit-reversed on the Class-1 wire.
        raw = bytes(tele_session._tele_reverse_bits8(value) for value in (0x00, 0x00, 0x7F))
        await sess.feed(raw)
        assert sess.mode is tele_session.TelebradescoMode.COMMAND
        assert sess.stage is tele_session.TelebradescoStage.AWAIT_DATA_FRM
        assert sess.frame_index == 0
        assert sess.sequence == 2

    asyncio.run(run())


def test_admin_malformed_content_length_returns_400_instead_of_dropping_connection(tmp_path):
    args = argparse.Namespace(
        admin_web=True,
        admin_host="127.0.0.1",
        admin_port=0,
        config=str(tmp_path / "config.json"),
    )
    httpd = admin_server.start_admin_server(args)
    assert httpd is not None
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.putrequest("POST", "/login")
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", "not-a-number")
        conn.endheaders()
        response = conn.getresponse()
        payload = response.read()
        assert response.status == 400
        assert b"requisi" in payload.lower() or b"inv" in payload.lower()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
