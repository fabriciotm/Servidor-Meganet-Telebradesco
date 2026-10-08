from __future__ import annotations

import argparse
import errno
import pathlib

import pytest

from meganet_server import cli


def _base_parse_args(tmp_path: pathlib.Path) -> list[str]:
    return [
        "--config", str(tmp_path / "meganet_config.json"),
        "--content-dir", str(tmp_path / "content"),
        "--meganet-transport", "off",
        "--telebradesco-transport", "off",
        "--no-admin-web",
    ]


@pytest.mark.parametrize(
    ("option", "filename"),
    [
        ("--tectoy-news-file", "missing-news.txt"),
        ("--connection-panel-file", "missing-panel.txt"),
        ("--message-file", "missing-message.txt"),
    ],
)
def test_configured_auxiliary_text_file_missing_is_controlled_startup_error(
    tmp_path: pathlib.Path,
    option: str,
    filename: str,
) -> None:
    missing = tmp_path / filename

    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args([*_base_parse_args(tmp_path), option, str(missing)])

    assert str(missing) in str(exc_info.value)
    assert "ler" in str(exc_info.value).lower()


def test_configured_auxiliary_text_file_invalid_utf8_is_controlled_startup_error(tmp_path: pathlib.Path) -> None:
    message_file = tmp_path / "message.txt"
    message_file.write_bytes(b"mensagem\xffinvalida")

    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args([*_base_parse_args(tmp_path), "--message-file", str(message_file)])

    assert str(message_file) in str(exc_info.value)
    assert "ler" in str(exc_info.value).lower()


def test_cli_main_returns_failure_code_on_listener_bind_error(monkeypatch, caplog) -> None:
    args = argparse.Namespace(log_level="INFO")
    monkeypatch.setattr(cli, "parse_args", lambda _argv: args)

    async def fail_run_services(_args, *, admin_server_factory):
        assert admin_server_factory is not None
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(cli, "run_services", fail_run_services)

    assert cli.main([]) == 1
    assert "STARTUP_FAILED" in caplog.text
    assert "Address already in use" in caplog.text

@pytest.mark.parametrize(
    ("mutator", "expected_fragment"),
    [
        (lambda value: [], "estrutura"),
        (lambda value: {**value, "conta_corrente": []}, "Conta Corrente"),
        (lambda value: {**value, "poupanca": "invalida"}, "Poupança"),
        (lambda value: {**value, "investimentos": {}}, "Investimentos"),
        (lambda value: {**value, "cartoes": {}}, "Cartões"),
        (lambda value: {**value, "cartoes": ["registro-invalido"]}, "Cartão 1"),
    ],
)
def test_telebradesco_admin_validation_rejects_malformed_top_level_structures(mutator, expected_fragment) -> None:
    from meganet_server.telebradesco.content import (
        blank_telebradesco_content,
        validate_telebradesco_content,
    )

    content = mutator(blank_telebradesco_content())

    with pytest.raises(ValueError, match=expected_fragment):
        validate_telebradesco_content(content)
