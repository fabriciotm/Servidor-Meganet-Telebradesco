from __future__ import annotations
import json
from meganet_server.storage import append_jsonl, atomic_write_json, read_json_dict


def test_atomic_json_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1, "texto": "ação"})
    assert read_json_dict(path, default={}) == {"a": 1, "texto": "ação"}
    assert not path.with_suffix(".json.tmp").exists()


def test_read_json_dict_returns_independent_default(tmp_path):
    default = {"items": []}
    loaded = read_json_dict(tmp_path / "missing.json", default=default)
    loaded["items"].append(1)
    assert default == {"items": []}


def test_append_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})
    assert [json.loads(x) for x in path.read_text().splitlines()] == [{"n": 1}, {"n": 2}]
