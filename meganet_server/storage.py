"""Small file-storage primitives shared by server components.

These helpers intentionally do not know anything about MegaNet or Telebradesco
schemas.  Domain validation remains in the respective repositories.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import uuid
from typing import Any


def read_json_dict(
    path: pathlib.Path,
    *,
    default: dict,
    log_prefix: str = "JSON",
    logger: logging.Logger | None = None,
) -> dict:
    """Read a JSON object and return a deep copy of *default* on failure."""
    path = pathlib.Path(path)
    log = logger or logging.getLogger(__name__)
    try:
        if path.exists() and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log.warning("%s_READ_FAILED path=%s error=%s", log_prefix, path, exc)
    return copy.deepcopy(default)


def atomic_write_text(path: pathlib.Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text using an exclusive sibling temporary file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(
    path: pathlib.Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 2,
    sort_keys: bool = False,
) -> None:
    """Serialize JSON and replace the destination atomically."""
    text = json.dumps(
        payload,
        ensure_ascii=ensure_ascii,
        indent=indent,
        sort_keys=sort_keys,
    )
    atomic_write_text(pathlib.Path(path), text, encoding="utf-8")


def append_jsonl(path: pathlib.Path, payload: Any, *, fsync: bool = False) -> None:
    """Append one JSON object to a JSONL file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        if fsync:
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
