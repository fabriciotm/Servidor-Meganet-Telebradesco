from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import threading

from meganet_server.meganet import content, content_store


def test_normalize_content_config_treats_string_fields_as_lines_and_null_as_default():
    cfg = content_store.normalize_content_config(
        {
            "connection_panel_lines": "PAINEL 1\nPAINEL 2",
            "message_lines": "MENSAGEM UNICA",
            "tectoy_news_lines": None,
            "content_records": {"internet.internet": "LINHA 1\nLINHA 2"},
        }
    )

    assert cfg["connection_panel_lines"] == ["PAINEL 1", "PAINEL 2"]
    assert cfg["message_lines"] == ["MENSAGEM UNICA"]
    assert cfg["tectoy_news_lines"] == content.DEFAULT_MESSAGE
    assert cfg["content_records"]["internet.internet"] == ["LINHA 1", "LINHA 2"]


def test_write_content_file_supports_concurrent_atomic_writers(tmp_path, monkeypatch):
    target = tmp_path / "content.json"
    original_replace = pathlib.Path.replace
    barrier = threading.Barrier(2)

    def synchronized_replace(self, target_path):
        if ".tmp" in self.name:
            barrier.wait(timeout=3)
        return original_replace(self, target_path)

    monkeypatch.setattr(pathlib.Path, "replace", synchronized_replace)

    cfg_a = content_store.default_content_config()
    cfg_b = content_store.default_content_config()
    cfg_a["message_lines"] = ["A"]
    cfg_b["message_lines"] = ["B"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(content_store.write_content_file, target, cfg_a),
            pool.submit(content_store.write_content_file, target, cfg_b),
        ]
        results = [future.result(timeout=5) for future in futures]

    assert results == [target, target]
    loaded = content_store.read_content_file(target, create_if_missing=False)
    assert loaded["message_lines"] in (["A"], ["B"])
    assert list(tmp_path.glob("*.tmp*")) == []


def test_content_file_selection_listing_and_runtime_propagation(tmp_path):
    base = tmp_path / "content"
    base.mkdir()
    (base / "b.json").write_text("{}", encoding="utf-8")
    (base / "A.JSON").write_text("{}", encoding="utf-8")
    (base / "ignore.txt").write_text("x", encoding="utf-8")

    files = content_store.list_content_json_files(base)
    assert files[0] == content.DEFAULT_CONTENT_FILE
    assert "A.JSON" in files and "b.json" in files and "ignore.txt" not in files
    assert content_store.sanitize_content_json_filename("../custom.json") == "custom.json"
    assert content_store.sanitize_content_json_filename("not-json.txt") == ""

    root = argparse.Namespace(content_dir=base, message_lines=["ROOT"])
    service = argparse.Namespace(message_lines=["OLD"])
    root._service_arg_refs = {"meganet": service, "telebradesco": argparse.Namespace(message_lines=["TELE"])}
    selected = content_store.select_content_json_file(root, "custom.json")
    assert selected == (base / "custom.json").resolve()

    cfg = content_store.default_content_config()
    cfg["message_lines"] = ["NOVO"]
    content_store.write_content_file(selected, cfg)
    content_store.reload_active_meganet_content(root, create_if_missing=False)
    assert root.message_lines == ["NOVO"]
    assert service.message_lines == ["NOVO"]


def test_public_content_normalizer_accepts_single_string_and_null_defensively():
    assert content.normalize_content_record_lines("LINHA 1\nLINHA 2") == ["LINHA 1", "LINHA 2"]
    assert content.normalize_content_record_lines(None) == ["SEM CONTEUDO"]


def test_content_budget_clamps_oversized_profile_without_dropping_records():
    defs = content.content_profile_definitions("tectoy-news")
    huge_line = "X" * content.MAX_MESSAGE_LINE_LEN
    profile_lines = {
        str(item["key"]): [huge_line] * content.MAX_CONTENT_RECORD_TOTAL_LINES
        for item in defs
    }

    fitted, before, after, clamped = content.fit_profile_lines_to_safe_budget(
        "tectoy-news", profile_lines
    )

    assert clamped is True
    assert before["total_bytes"] > content.MAX_CONTENT_GROUP_SAFE_BYTES
    assert after["total_bytes"] <= content.MAX_CONTENT_GROUP_SAFE_BYTES
    assert set(fitted) == {str(item["key"]) for item in defs}
    assert all(fitted[key] for key in fitted)
    assert all(len(lines) >= 1 for lines in fitted.values())


def test_content_markers_placeholders_and_display_wrapping_are_preserved():
    cleaned = content.strip_content_mapping_lines(
        ["MAP-00", "LINHA", "", "[BR]", "#HASH#", "AÇÃO"]
    )
    assert cleaned[0] == "LINHA"
    assert cleaned[1:3] == [content.CONTENT_BLANK_LINE_MARKER] * 2
    assert "MAP-00" not in cleaned

    rendered = content.render_meganet_placeholder_lines(
        "OLA {usuario}\nEMAIL {email}",
        {"nome_meganet": "João"},
        mail_domain="meganet.tectoy.com.br",
        max_lines=10,
    )
    assert rendered[0] == "OLA João"
    assert rendered[1].startswith("EMAIL jo")

    display = content.prepare_display_lines(
        ["PRIMEIRA", "<BR>", "TOKENMUITOLONGO1234567890ABCDEFGHIJ"],
        12,
    )
    assert display[0] == "PRIMEIRA"
    assert display[1] == ""
    assert all(len(line) <= 12 for line in display[2:])
    assert content.content_line_wire_bytes("<BR>") == 1
