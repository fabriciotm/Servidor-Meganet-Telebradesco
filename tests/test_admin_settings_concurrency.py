from __future__ import annotations

import argparse
import http.client
import json
import pathlib
import re
import threading
import time
import urllib.parse

from meganet_server import config as config_module
from meganet_server import server as server_module
from meganet_server.admin.controllers import settings as settings_module
from meganet_server.admin.controllers.settings import SettingsControllerMixin
from meganet_server.config import default_operational_config, write_config_file
from meganet_server.meganet.content_store import read_content_file
from meganet_server.runtime.configuration import service_args


class _Context:
    def __init__(self, args: argparse.Namespace, config_path: pathlib.Path) -> None:
        self.args = args
        self.config_path = config_path


class _Harness(SettingsControllerMixin):
    def __init__(self, context: _Context) -> None:
        self.app_context = context
        self.sent: list[tuple[int, bytes, str]] = []
        self.redirects: list[str] = []

    def _send(self, status: int, payload, content_type: str = "text/html; charset=utf-8"):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.sent.append((status, payload, content_type))
        return status, payload, content_type

    def _redirect(self, destination: str):
        self.redirects.append(destination)
        return destination


def _args(tmp_path: pathlib.Path) -> argparse.Namespace:
    values = default_operational_config()
    values.update(
        {
            "config": str(tmp_path / "meganet_config.json"),
            "content_dir": tmp_path / "meganet" / "content.json",
            "cadastro_db": tmp_path / "meganet" / "cadastros.json",
            "mail_store_dir": tmp_path / "meganet" / "mail",
            "mail_inbox": tmp_path / "meganet" / "mail" / "inbox.json",
            "output_dir": tmp_path / "sessions",
            "password_change_store_dir": tmp_path / "password",
            "telebradesco_accounts_file": tmp_path / "telebradesco" / "accounts.json",
            "telebradesco_content_file": tmp_path / "telebradesco" / "accounts_content.json",
            "active_service": "meganet",
        }
    )
    return argparse.Namespace(**values)


def _parsed(path: str):
    return urllib.parse.urlparse(path)


def _post_http(host: str, port: int, path: str, form: dict[str, str], cookie: str):
    body = urllib.parse.urlencode(form)
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        payload = response.read()
        return response.status, payload
    finally:
        conn.close()


def test_threading_http_service_select_keeps_namespace_and_config_consistent(tmp_path, monkeypatch) -> None:
    args = server_module.parse_args(
        [
            "--config", str(tmp_path / "meganet_config.json"),
            "--admin-host", "127.0.0.1",
            "--admin-port", "0",
        ]
    )
    httpd = server_module.start_admin_server(args)
    assert httpd is not None
    original_atomic = config_module.atomic_write_json
    first_captured = threading.Event()
    second_entered = threading.Event()

    def ordered_atomic(path, data, **kwargs):
        path = pathlib.Path(path)
        if path == pathlib.Path(args.config):
            service = data.get("active_service") if isinstance(data, dict) else None
            if service == "telebradesco" and not first_captured.is_set():
                first_captured.set()
                second_entered.wait(0.30)
            elif service == "meganet" and first_captured.is_set():
                result = original_atomic(path, data, **kwargs)
                second_entered.set()
                return result
        return original_atomic(path, data, **kwargs)

    monkeypatch.setattr(config_module, "atomic_write_json", ordered_atomic)

    try:
        context = httpd.RequestHandlerClass.app_context
        token = context.auth_manager.create_session("admin")
        cookie = f"SMT_ADMIN_SESSION={urllib.parse.quote(token)}"
        host, port = httpd.server_address[:2]

        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("GET", "/login")
            response = conn.getresponse()
            login_html = response.read()
        finally:
            conn.close()
        match = re.search(rb"name='_csrf' value='([^']+)'", login_html)
        assert match
        csrf = match.group(1).decode("utf-8")

        result_a: list[tuple[int, bytes]] = []
        result_b: list[tuple[int, bytes]] = []
        thread_a = threading.Thread(
            target=lambda: result_a.append(
                _post_http(host, port, "/service/select", {"_csrf": csrf, "active_service": "telebradesco"}, cookie)
            ),
            name="settings-service-A",
        )
        thread_b = threading.Thread(
            target=lambda: result_b.append(
                _post_http(host, port, "/service/select", {"_csrf": csrf, "active_service": "meganet"}, cookie)
            ),
            name="settings-service-B",
        )
        thread_a.start()
        assert first_captured.wait(2)
        thread_b.start()
        thread_a.join(5)
        thread_b.join(5)
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert result_a and result_b
        assert result_a[0][0] == 303
        assert result_b[0][0] == 303

        persisted = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
        assert persisted["active_service"] == args.active_service
        assert args.active_service == "meganet"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_settings_failed_request_cannot_rollback_successful_concurrent_request(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    config_path = pathlib.Path(args.config)
    write_config_file(config_path, args)
    original_write = settings_module.write_config_file
    first_entered = threading.Event()
    second_done = threading.Event()

    def ordered_write(path, namespace):
        if threading.current_thread().name == "settings-failing-A":
            first_entered.set()
            second_done.wait(0.30)
            raise OSError("simulated disk failure")
        result = original_write(path, namespace)
        second_done.set()
        return result

    monkeypatch.setattr(settings_module, "write_config_file", ordered_write)
    handler_a = _Harness(_Context(args, config_path))
    handler_b = _Harness(_Context(args, config_path))

    old_email = args.admin_email
    thread_a = threading.Thread(
        target=lambda: handler_a._post_settings(
            _parsed("/settings"), {"admin_email": ["first@example.test"]}, {"role": "admin"}
        ),
        name="settings-failing-A",
    )
    thread_b = threading.Thread(
        target=lambda: handler_b._post_settings(
            _parsed("/settings"), {"mail_domain": ["concurrent.example"]}, {"role": "admin"}
        ),
        name="settings-success-B",
    )
    thread_a.start()
    assert first_entered.wait(2)
    thread_b.start()
    thread_a.join(5)
    thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert handler_a.sent[-1][0] == 500
    assert handler_b.sent[-1][0] == 200

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert args.admin_email == old_email
    assert args.mail_domain == "concurrent.example"
    assert persisted["admin_email"] == args.admin_email
    assert persisted["mail_domain"] == args.mail_domain


def test_content_save_failure_cannot_rollback_successful_concurrent_content_update(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    clone = service_args(args, "meganet")
    original_write_content = settings_module.write_content_file
    first_entered = threading.Event()
    second_done = threading.Event()

    def ordered_write_content(path, namespace):
        if threading.current_thread().name == "content-failing-A":
            first_entered.set()
            second_done.wait(0.30)
            raise OSError("simulated content failure")
        result = original_write_content(path, namespace)
        second_done.set()
        return result

    monkeypatch.setattr(settings_module, "write_content_file", ordered_write_content)
    handler_a = _Harness(_Context(args, pathlib.Path(args.config)))
    handler_b = _Harness(_Context(args, pathlib.Path(args.config)))

    thread_a = threading.Thread(
        target=lambda: handler_a._post_content_save(
            _parsed("/content/save"), {"message_lines": ["MENSAGEM A"]}, {"role": "admin"}
        ),
        name="content-failing-A",
    )
    thread_b = threading.Thread(
        target=lambda: handler_b._post_content_save(
            _parsed("/content/save"), {"message_lines": ["MENSAGEM B"]}, {"role": "admin"}
        ),
        name="content-success-B",
    )
    thread_a.start()
    assert first_entered.wait(2)
    thread_b.start()
    thread_a.join(5)
    thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert handler_a.sent[-1][0] == 500
    assert handler_b.redirects[-1].startswith("/content?saved=1")

    persisted = read_content_file(args.content_dir, create_if_missing=False)
    assert args.message_lines == ["MENSAGEM B"]
    assert clone.message_lines == ["MENSAGEM B"]
    assert persisted["message_lines"] == ["MENSAGEM B"]


def test_reload_cannot_erase_concurrent_successful_settings_save(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)
    config_path = pathlib.Path(args.config)
    write_config_file(config_path, args)
    original_write = settings_module.write_config_file
    first_entered = threading.Event()
    reload_done = threading.Event()

    def delayed_write(path, namespace):
        if threading.current_thread().name == "settings-save-A":
            first_entered.set()
            reload_done.wait(0.30)
        return original_write(path, namespace)

    monkeypatch.setattr(settings_module, "write_config_file", delayed_write)
    handler_a = _Harness(_Context(args, config_path))
    handler_b = _Harness(_Context(args, config_path))
    original_email = args.admin_email

    def do_reload():
        handler_b._post_reload(_parsed("/reload"), {"return_to": ["/settings"]}, {"role": "admin"})
        reload_done.set()

    thread_a = threading.Thread(
        target=lambda: handler_a._post_settings(
            _parsed("/settings"), {"admin_email": ["saved@example.test"]}, {"role": "admin"}
        ),
        name="settings-save-A",
    )
    thread_b = threading.Thread(target=do_reload, name="settings-reload-B")
    thread_a.start()
    assert first_entered.wait(2)
    thread_b.start()
    thread_a.join(5)
    thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert handler_a.sent[-1][0] == 200
    assert handler_b.sent[-1][0] == 200

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert args.admin_email == "saved@example.test", (original_email, args.admin_email, persisted["admin_email"])
    assert persisted["admin_email"] == "saved@example.test"
