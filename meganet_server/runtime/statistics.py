"""Persistent runtime access/traffic statistics and active-session registry."""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import logging
import pathlib
import re
import secrets
import threading
from typing import Optional

from ..common import format_datetime as _format_admin_datetime
from ..storage import atomic_write_json, read_json_dict

DEFAULT_RUNTIME_LOG_DIR = pathlib.Path("logs")
DEFAULT_RUNTIME_ACCESS_LOG_DIR = DEFAULT_RUNTIME_LOG_DIR / "access"
DEFAULT_RUNTIME_TRAFFIC_LOG_DIR = DEFAULT_RUNTIME_LOG_DIR / "traffic"
RUNTIME_LOG_LOCK = threading.RLock()
_RUNTIME_PENDING_TRAFFIC: dict[pathlib.Path, dict] = {}
_RUNTIME_PENDING_ACCESS: dict[pathlib.Path, dict] = {}

def _runtime_now_local() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def _runtime_log_date_key(dt: Optional[_dt.datetime] = None) -> str:
    return (dt or _runtime_now_local()).strftime("%Y%m%d")


def _runtime_log_date_label(date_key: str) -> str:
    text = str(date_key or "")
    if re.match(r"^\d{8}$", text):
        return f"{text[6:8]}/{text[4:6]}/{text[0:4]}"
    return text


def _runtime_access_log_path(date_key: Optional[str] = None) -> pathlib.Path:
    key = date_key or _runtime_log_date_key()
    return DEFAULT_RUNTIME_ACCESS_LOG_DIR / f"access_{key}.json"


def _runtime_traffic_log_path(date_key: Optional[str] = None) -> pathlib.Path:
    key = date_key or _runtime_log_date_key()
    return DEFAULT_RUNTIME_TRAFFIC_LOG_DIR / f"traffic_{key}.json"


def _runtime_load_json(path: pathlib.Path, default: dict) -> dict:
    return read_json_dict(
        path,
        default=default,
        log_prefix="RUNTIME_LOG",
        logger=logging.getLogger(__name__),
    )


def _runtime_write_json(path: pathlib.Path, payload: dict) -> bool:
    try:
        atomic_write_json(path, payload, ensure_ascii=False, indent=2)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("RUNTIME_LOG_WRITE_FAILED path=%s error=%s", path, exc)
        return False


def _runtime_default_access_payload(date_key: Optional[str] = None) -> dict:
    key = date_key or _runtime_log_date_key()
    return {"schema": "runtime_access_daily_v1", "date": key, "connections": [], "events": [], "updated_at": ""}


def _runtime_default_traffic_payload(date_key: Optional[str] = None) -> dict:
    key = date_key or _runtime_log_date_key()
    return {
        "schema": "runtime_traffic_daily_v1",
        "date": key,
        "totals": {"meganet": {"tx": 0, "rx": 0}, "telebradesco": {"tx": 0, "rx": 0}},
        "buckets": [],
        "updated_at": "",
    }


def _runtime_bucket_key(dt: Optional[_dt.datetime] = None) -> str:
    return (dt or _runtime_now_local()).strftime("%Y-%m-%d %H:%M")


def _runtime_bucket_label(key: object) -> str:
    text = str(key or "")
    if len(text) >= 16:
        return text[-5:]
    return text


def _runtime_safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _runtime_new_connection_id(service: str) -> str:
    now = _dt.datetime.now()
    suffix = secrets.token_hex(3)
    return f"{service}-{now.strftime('%Y%m%d%H%M%S')}-{now.microsecond // 1000:03d}-{suffix}"


def _runtime_access_event_key(event: dict) -> tuple[str, str, str, str, str]:
    return (
        str(event.get("connection_id") or ""),
        str(event.get("event") or ""),
        str(event.get("time") or ""),
        str(event.get("service") or ""),
        str(event.get("peer") or ""),
    )


def _runtime_pending_access_locked(path: pathlib.Path, date_key: str) -> dict:
    return _RUNTIME_PENDING_ACCESS.setdefault(path, {
        "date": date_key,
        "connections": {},
        "events": [],
        "identity_updates": {},
        "updated_at": "",
    })


def _runtime_flush_pending_access_locked(path: pathlib.Path, pending: dict) -> bool:
    date_key = str(pending.get("date") or path.stem.replace("access_", "") or _runtime_log_date_key())
    payload = _runtime_load_json(path, _runtime_default_access_payload(date_key))

    connections = payload.get("connections") if isinstance(payload.get("connections"), list) else []
    by_id = {
        str(item.get("id") or ""): index
        for index, item in enumerate(connections)
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    pending_connections = pending.get("connections") if isinstance(pending.get("connections"), dict) else {}
    for rec_id, record in pending_connections.items():
        if not isinstance(record, dict):
            continue
        clean = dict(record)
        if rec_id and rec_id in by_id:
            connections[by_id[rec_id]] = clean
        else:
            connections.append(clean)
            if rec_id:
                by_id[rec_id] = len(connections) - 1

    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    event_keys = {_runtime_access_event_key(item) for item in events if isinstance(item, dict)}
    pending_events = pending.get("events") if isinstance(pending.get("events"), list) else []
    for event in pending_events:
        if not isinstance(event, dict):
            continue
        key = _runtime_access_event_key(event)
        if key not in event_keys:
            events.append(dict(event))
            event_keys.add(key)

    identity_updates = pending.get("identity_updates") if isinstance(pending.get("identity_updates"), dict) else {}
    for connection_id, update in identity_updates.items():
        if not isinstance(update, dict):
            continue
        identity = str(update.get("identity") or "")
        service = str(update.get("service") or "")
        peer = str(update.get("peer") or "")
        for item in connections:
            if isinstance(item, dict) and str(item.get("id") or "") == connection_id:
                item["identity"] = identity
                if service:
                    item["service"] = service
                if peer:
                    item["peer"] = peer
        for item in events:
            if isinstance(item, dict) and str(item.get("connection_id") or "") == connection_id:
                item["identity"] = identity

    payload["connections"] = connections
    payload["events"] = events
    payload["updated_at"] = str(pending.get("updated_at") or _runtime_now_local().isoformat(timespec="seconds"))
    return _runtime_write_json(path, payload)


def _runtime_flush_all_pending_access_locked() -> None:
    for pending_path, pending_data in list(_RUNTIME_PENDING_ACCESS.items()):
        if _runtime_flush_pending_access_locked(pending_path, pending_data):
            _RUNTIME_PENDING_ACCESS.pop(pending_path, None)


def _runtime_append_access_event(event: dict) -> None:
    now = _runtime_now_local()
    date_key = _runtime_log_date_key(now)
    event = dict(event or {})
    event.setdefault("time", now.isoformat(timespec="seconds"))
    event.setdefault("date", date_key)
    with RUNTIME_LOG_LOCK:
        path = _runtime_access_log_path(date_key)
        pending = _runtime_pending_access_locked(path, date_key)
        key = _runtime_access_event_key(event)
        if key not in {_runtime_access_event_key(item) for item in pending["events"] if isinstance(item, dict)}:
            pending["events"].append(event)
        pending["updated_at"] = now.isoformat(timespec="seconds")
        _runtime_flush_all_pending_access_locked()


def _runtime_upsert_connection(record: dict) -> None:
    if not isinstance(record, dict):
        return
    record = dict(record)
    start_text = str(record.get("start_at") or record.get("time") or "")
    date_key = str(record.get("date") or "")
    if not date_key:
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", start_text)
        date_key = f"{match.group(1)}{match.group(2)}{match.group(3)}" if match else _runtime_log_date_key()
    record["date"] = date_key
    with RUNTIME_LOG_LOCK:
        path = _runtime_access_log_path(date_key)
        pending = _runtime_pending_access_locked(path, date_key)
        rec_id = str(record.get("id") or "")
        if rec_id:
            pending["connections"][rec_id] = record
        else:
            # Connection records created by the runtime always have an id; retain
            # a stable fallback key for defensive callers that do not.
            pending["connections"][f"anonymous:{len(pending['connections'])}"] = record
        pending["updated_at"] = _runtime_now_local().isoformat(timespec="seconds")
        _runtime_flush_all_pending_access_locked()


def _runtime_update_connection_identity(connection_id: str, service: str, peer: str, identity: str) -> None:
    if not connection_id:
        return
    today = _runtime_log_date_key()
    with RUNTIME_LOG_LOCK:
        # Retry anything already queued before looking at persisted files. A pending
        # start event/connection may be the only copy while the filesystem is down.
        for pending in _RUNTIME_PENDING_ACCESS.values():
            pending_connections = pending.get("connections") if isinstance(pending.get("connections"), dict) else {}
            pending_events = pending.get("events") if isinstance(pending.get("events"), list) else []
            if connection_id in pending_connections or any(
                isinstance(item, dict) and str(item.get("connection_id") or "") == connection_id
                for item in pending_events
            ):
                pending.setdefault("identity_updates", {})[connection_id] = {
                    "service": service, "peer": peer, "identity": str(identity or "")
                }
                record = pending_connections.get(connection_id)
                if isinstance(record, dict):
                    record["identity"] = str(identity or "")
                    record["service"] = service
                    record["peer"] = peer
                for item in pending_events:
                    if isinstance(item, dict) and str(item.get("connection_id") or "") == connection_id:
                        item["identity"] = str(identity or "")

        candidates = set(_runtime_log_paths(DEFAULT_RUNTIME_ACCESS_LOG_DIR, "access_*.json")[-3:])
        candidates.update(_RUNTIME_PENDING_ACCESS.keys())
        candidates.add(_runtime_access_log_path(today))
        for path in sorted(candidates, key=str):
            payload = _runtime_load_json(path, _runtime_default_access_payload(path.stem.replace("access_", "")))
            changed = False
            connections = payload.get("connections") if isinstance(payload.get("connections"), list) else []
            for item in connections:
                if isinstance(item, dict) and str(item.get("id") or "") == connection_id:
                    item["identity"] = str(identity or "")
                    item["service"] = service
                    item["peer"] = peer
                    changed = True
            events = payload.get("events") if isinstance(payload.get("events"), list) else []
            for item in events:
                if isinstance(item, dict) and str(item.get("connection_id") or "") == connection_id:
                    item["identity"] = str(identity or "")
                    changed = True
            if changed:
                payload["updated_at"] = _runtime_now_local().isoformat(timespec="seconds")
                if not _runtime_write_json(path, payload):
                    date_key = path.stem.replace("access_", "")
                    pending = _runtime_pending_access_locked(path, date_key)
                    pending.setdefault("identity_updates", {})[connection_id] = {
                        "service": service, "peer": peer, "identity": str(identity or "")
                    }
                    pending["updated_at"] = payload["updated_at"]
        _runtime_flush_all_pending_access_locked()

def _runtime_flush_pending_traffic_locked(path: pathlib.Path, pending: dict) -> bool:
    date_key = str(pending.get("date") or path.stem.replace("traffic_", "") or _runtime_log_date_key())
    payload = _runtime_load_json(path, _runtime_default_traffic_payload(date_key))
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    pending_totals = pending.get("totals") if isinstance(pending.get("totals"), dict) else {}
    for svc in ("meganet", "telebradesco"):
        totals.setdefault(svc, {"tx": 0, "rx": 0})
        if not isinstance(totals[svc], dict):
            totals[svc] = {"tx": 0, "rx": 0}
        svc_pending = pending_totals.get(svc) if isinstance(pending_totals.get(svc), dict) else {}
        for item_direction in ("tx", "rx"):
            totals[svc][item_direction] = (
                _runtime_safe_int(totals[svc].get(item_direction))
                + _runtime_safe_int(svc_pending.get(item_direction))
            )

    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), list) else []
    buckets_by_key = {
        str(item.get("key") or ""): item
        for item in buckets
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    pending_buckets = pending.get("buckets") if isinstance(pending.get("buckets"), dict) else {}
    for pending_key, delta in pending_buckets.items():
        if not isinstance(delta, dict):
            continue
        bucket = buckets_by_key.get(str(pending_key))
        if bucket is None:
            bucket = {
                "key": str(pending_key),
                "label": str(delta.get("label") or _runtime_bucket_label(pending_key)),
                "meganet_tx": 0,
                "meganet_rx": 0,
                "telebradesco_tx": 0,
                "telebradesco_rx": 0,
            }
            buckets.append(bucket)
            buckets_by_key[str(pending_key)] = bucket
        for field in ("meganet_tx", "meganet_rx", "telebradesco_tx", "telebradesco_rx"):
            bucket[field] = _runtime_safe_int(bucket.get(field)) + _runtime_safe_int(delta.get(field))

    buckets.sort(key=lambda item: str(item.get("key") if isinstance(item, dict) else ""))
    payload["totals"] = totals
    payload["buckets"] = buckets[-1440:]
    payload["updated_at"] = str(pending.get("updated_at") or _runtime_now_local().isoformat(timespec="seconds"))
    return _runtime_write_json(path, payload)


def _runtime_record_traffic_persistent(service: str, direction: str, size: int) -> None:
    if size <= 0:
        return
    service = service if service in {"meganet", "telebradesco"} else "meganet"
    direction = "tx" if direction == "tx" else "rx"
    now = _runtime_now_local()
    date_key = _runtime_log_date_key(now)
    key = _runtime_bucket_key(now)
    bucket_field = f"{service}_{direction}"
    path = _runtime_traffic_log_path(date_key)
    with RUNTIME_LOG_LOCK:
        pending = _RUNTIME_PENDING_TRAFFIC.setdefault(path, {
            "date": date_key,
            "updated_at": now.isoformat(timespec="seconds"),
            "totals": {"meganet": {"tx": 0, "rx": 0}, "telebradesco": {"tx": 0, "rx": 0}},
            "buckets": {},
        })
        pending["date"] = date_key
        pending["updated_at"] = now.isoformat(timespec="seconds")
        pending["totals"][service][direction] += int(size)
        pending_bucket = pending["buckets"].setdefault(key, {
            "key": key,
            "label": _runtime_bucket_label(key),
            "meganet_tx": 0,
            "meganet_rx": 0,
            "telebradesco_tx": 0,
            "telebradesco_rx": 0,
        })
        pending_bucket[bucket_field] += int(size)

        # A recovered filesystem must flush not only the current delta but also
        # pending traffic from previous daily partitions (for example a write
        # that failed immediately before midnight). Each pending date is kept in
        # its original file, preserving daily rotation semantics.
        for pending_path, pending_data in list(_RUNTIME_PENDING_TRAFFIC.items()):
            if _runtime_flush_pending_traffic_locked(pending_path, pending_data):
                _RUNTIME_PENDING_TRAFFIC.pop(pending_path, None)


def _runtime_log_paths(directory: pathlib.Path, pattern: str) -> list[pathlib.Path]:
    try:
        return sorted([p for p in directory.glob(pattern) if p.is_file()])
    except OSError:
        return []


def _runtime_load_today_traffic() -> tuple[dict[str, dict[str, int]], list[dict]]:
    date_key = _runtime_log_date_key()
    payload = _runtime_load_json(_runtime_traffic_log_path(date_key), _runtime_default_traffic_payload(date_key))
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    result_totals = {"meganet": {"tx": 0, "rx": 0}, "telebradesco": {"tx": 0, "rx": 0}}
    for svc in result_totals:
        data = totals.get(svc) if isinstance(totals.get(svc), dict) else {}
        for direction in ("tx", "rx"):
            result_totals[svc][direction] = _runtime_safe_int(data.get(direction))
    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), list) else []
    clean_buckets = []
    for item in buckets[-UnifiedServiceRuntime.TRAFFIC_BUCKETS:] if 'UnifiedServiceRuntime' in globals() else buckets[-12:]:
        if isinstance(item, dict):
            clean_buckets.append({
                "key": str(item.get("key") or ""),
                "label": str(item.get("label") or _runtime_bucket_label(item.get("key"))),
                "meganet_tx": _runtime_safe_int(item.get("meganet_tx")),
                "meganet_rx": _runtime_safe_int(item.get("meganet_rx")),
                "telebradesco_tx": _runtime_safe_int(item.get("telebradesco_tx")),
                "telebradesco_rx": _runtime_safe_int(item.get("telebradesco_rx")),
            })
    return result_totals, clean_buckets


def _runtime_load_recent_accesses(limit: int = 20) -> list[dict]:
    rows: list[dict] = []
    with RUNTIME_LOG_LOCK:
        paths = _runtime_log_paths(DEFAULT_RUNTIME_ACCESS_LOG_DIR, "access_*.json")[-7:]
        for path in paths:
            payload = _runtime_load_json(path, _runtime_default_access_payload(path.stem.replace("access_", "")))
            events = payload.get("events") if isinstance(payload.get("events"), list) else []
            for item in events:
                if not isinstance(item, dict):
                    continue
                rows.append({
                    "time_iso": str(item.get("time") or ""),
                    "time": _format_admin_datetime(item.get("time")),
                    "service": str(item.get("service") or ""),
                    "peer": str(item.get("peer") or ""),
                    "identity": str(item.get("identity") or ""),
                    "event": str(item.get("event") or "Acesso"),
                    "tx_bytes": _runtime_safe_int(item.get("tx_bytes")),
                    "rx_bytes": _runtime_safe_int(item.get("rx_bytes")),
                })
    rows.sort(key=lambda x: str(x.get("time_iso") or ""), reverse=True)
    return rows[:limit]


def _runtime_parse_date(value: object, default: _dt.date) -> _dt.date:
    text = str(value or "").strip()
    if not text:
        return default
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return default


def _runtime_parse_time(value: object, default: _dt.time) -> _dt.time:
    text = str(value or "").strip()
    if not text:
        return default
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    return default


def _runtime_parse_iso_local(value: object) -> Optional[_dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", text)
    if match:
        y, m, d, hh, mm, ss = match.groups()
        try:
            return _dt.datetime(int(y), int(m), int(d), int(hh), int(mm), int(ss or 0))
        except ValueError:
            return None
    return None


def _runtime_date_keys_between(start_date: _dt.date, end_date: _dt.date) -> list[str]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    days = []
    current = start_date
    # Logs são particionados por dia. O limite alto protege contra intervalos
    # acidentais absurdos sem impedir a consulta do histórico persistente real.
    for _ in range(36525):
        if current > end_date:
            break
        days.append(current.strftime("%Y%m%d"))
        current += _dt.timedelta(days=1)
    return days


def _runtime_access_date_range() -> tuple[_dt.date, _dt.date]:
    """Return the oldest/newest persisted access-log dates, falling back to today."""
    today = _dt.date.today()
    dates: list[_dt.date] = []
    for path in _runtime_log_paths(DEFAULT_RUNTIME_ACCESS_LOG_DIR, "access_*.json"):
        match = re.fullmatch(r"access_(\d{8})\.json", path.name)
        if not match:
            continue
        try:
            dates.append(_dt.datetime.strptime(match.group(1), "%Y%m%d").date())
        except ValueError:
            continue
    if not dates:
        return today, today
    return min(dates), max(max(dates), today)


def _runtime_query_statistics(params: dict[str, list[str]]) -> dict:
    today = _dt.date.today()
    start_date = _runtime_parse_date(params.get("date_from", [""])[0] if isinstance(params.get("date_from"), list) else "", today)
    end_date = _runtime_parse_date(params.get("date_to", [""])[0] if isinstance(params.get("date_to"), list) else "", start_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_time = _runtime_parse_time(params.get("time_from", [""])[0] if isinstance(params.get("time_from"), list) else "", _dt.time(0, 0))
    end_time = _runtime_parse_time(params.get("time_to", [""])[0] if isinstance(params.get("time_to"), list) else "", _dt.time(23, 59, 59))
    service_filter = str(params.get("service", ["all"])[0] if isinstance(params.get("service"), list) else "all").strip().lower()
    if service_filter not in {"all", "meganet", "telebradesco"}:
        service_filter = "all"
    origin_filter = str(params.get("origin", [""])[0] if isinstance(params.get("origin"), list) else "").strip().casefold()
    user_filter = str(params.get("user", [""])[0] if isinstance(params.get("user"), list) else "").strip().casefold()
    start_dt = _dt.datetime.combine(start_date, start_time)
    end_dt = _dt.datetime.combine(end_date, end_time)
    buckets_by_key: dict[str, dict] = {}
    connections: list[dict] = []
    events: list[dict] = []
    totals = {"meganet": {"tx": 0, "rx": 0}, "telebradesco": {"tx": 0, "rx": 0}}
    for date_key in _runtime_date_keys_between(start_date, end_date):
        traffic_payload = _runtime_load_json(_runtime_traffic_log_path(date_key), _runtime_default_traffic_payload(date_key))
        for item in traffic_payload.get("buckets", []) if isinstance(traffic_payload.get("buckets"), list) else []:
            if not isinstance(item, dict):
                continue
            bucket_dt = _runtime_parse_iso_local(str(item.get("key") or "").replace(" ", "T"))
            if bucket_dt is None or bucket_dt < start_dt or bucket_dt > end_dt:
                continue
            clean = buckets_by_key.setdefault(str(item.get("key") or ""), {"key": str(item.get("key") or ""), "label": str(item.get("label") or _runtime_bucket_label(item.get("key"))), "meganet_tx": 0, "meganet_rx": 0, "telebradesco_tx": 0, "telebradesco_rx": 0})
            for field in ("meganet_tx", "meganet_rx", "telebradesco_tx", "telebradesco_rx"):
                clean[field] += _runtime_safe_int(item.get(field))
        access_payload = _runtime_load_json(_runtime_access_log_path(date_key), _runtime_default_access_payload(date_key))
        for item in access_payload.get("connections", []) if isinstance(access_payload.get("connections"), list) else []:
            if not isinstance(item, dict):
                continue
            started = _runtime_parse_iso_local(item.get("start_at")) or _runtime_parse_iso_local(item.get("time"))
            if started is None or started < start_dt or started > end_dt:
                continue
            service = str(item.get("service") or "")
            origin_text = " ".join([str(item.get("peer") or ""), str(item.get("identity") or "")]).casefold()
            if service_filter != "all" and service != service_filter:
                continue
            if origin_filter and origin_filter not in origin_text:
                continue
            identity_text = str(item.get("identity") or "").casefold()
            if user_filter and user_filter not in identity_text:
                continue
            clean = dict(item)
            for field in ("tx_bytes", "rx_bytes", "duration_seconds", "avg_bps"):
                clean[field] = _runtime_safe_int(clean.get(field))
            clean["start_display"] = _format_admin_datetime(clean.get("start_at"))
            clean["end_display"] = _format_admin_datetime(clean.get("end_at")) if clean.get("end_at") else "-"
            connections.append(clean)
        for item in access_payload.get("events", []) if isinstance(access_payload.get("events"), list) else []:
            if not isinstance(item, dict):
                continue
            happened = _runtime_parse_iso_local(item.get("time"))
            if happened is None or happened < start_dt or happened > end_dt:
                continue
            service = str(item.get("service") or "")
            origin_text = " ".join([str(item.get("peer") or ""), str(item.get("identity") or "")]).casefold()
            if service_filter != "all" and service != service_filter:
                continue
            if origin_filter and origin_filter not in origin_text:
                continue
            identity_text = str(item.get("identity") or "").casefold()
            if user_filter and user_filter not in identity_text:
                continue
            clean = dict(item)
            for field in ("tx_bytes", "rx_bytes", "duration_seconds", "avg_bps"):
                if field in clean:
                    clean[field] = _runtime_safe_int(clean.get(field))
            clean["time_display"] = _format_admin_datetime(clean.get("time"))
            events.append(clean)
    buckets = [buckets_by_key[key] for key in sorted(buckets_by_key)]
    for bucket in buckets:
        if service_filter in {"all", "meganet"}:
            totals["meganet"]["tx"] += _runtime_safe_int(bucket.get("meganet_tx"))
            totals["meganet"]["rx"] += _runtime_safe_int(bucket.get("meganet_rx"))
        if service_filter in {"all", "telebradesco"}:
            totals["telebradesco"]["tx"] += _runtime_safe_int(bucket.get("telebradesco_tx"))
            totals["telebradesco"]["rx"] += _runtime_safe_int(bucket.get("telebradesco_rx"))
    connections.sort(key=lambda item: str(item.get("start_at") or ""), reverse=True)
    events.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    return {"start_date": start_date, "end_date": end_date, "start_time": start_time, "end_time": end_time, "service": service_filter, "origin": origin_filter, "user": user_filter, "buckets": buckets, "connections": connections, "events": events, "totals": totals}


class UnifiedServiceRuntime:
    TRAFFIC_BUCKETS = 12

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._lock = threading.Lock()
        self._writers: dict[int, tuple[object, str]] = {}
        loaded_traffic, loaded_buckets = _runtime_load_today_traffic()
        self._traffic_date_key = _runtime_log_date_key()
        self._traffic: dict[str, dict[str, int]] = loaded_traffic
        self._traffic_buckets: list[dict] = loaded_buckets[-self.TRAFFIC_BUCKETS:]
        self._recent_accesses: list[dict] = _runtime_load_recent_accesses(20)
        self._access_counts: dict[str, int] = {"total": 0, "meganet": 0, "telebradesco": 0}
        self._access_identities: dict[tuple[str, str], str] = {}
        self._active_connections: dict[int, dict] = {}

    @staticmethod
    def _minute_key(now: Optional[_dt.datetime] = None) -> str:
        current = now or _dt.datetime.now()
        return current.strftime("%Y-%m-%d %H:%M")

    def _ensure_current_day_locked(self) -> None:
        current_date_key = _runtime_log_date_key()
        if current_date_key == self._traffic_date_key:
            return
        loaded_traffic, loaded_buckets = _runtime_load_today_traffic()
        self._traffic_date_key = current_date_key
        self._traffic = loaded_traffic
        self._traffic_buckets = loaded_buckets[-self.TRAFFIC_BUCKETS:]

    def _ensure_bucket_locked(self) -> dict:
        self._ensure_current_day_locked()
        key = self._minute_key()
        if not self._traffic_buckets or self._traffic_buckets[-1]["key"] != key:
            self._traffic_buckets.append({
                "key": key,
                "label": key[-5:],
                "meganet_tx": 0,
                "meganet_rx": 0,
                "telebradesco_tx": 0,
                "telebradesco_rx": 0,
            })
            self._traffic_buckets = self._traffic_buckets[-self.TRAFFIC_BUCKETS:]
        return self._traffic_buckets[-1]

    def register(self, writer: object, service: str, peer: str = "unknown") -> None:
        service = service if service in {"meganet", "telebradesco"} else "meganet"
        now = _runtime_now_local()
        connection_id = _runtime_new_connection_id(service)
        identity = self._access_identities.get((service, peer), "")
        record = {
            "id": connection_id,
            "date": _runtime_log_date_key(now),
            "service": service,
            "peer": str(peer or "unknown"),
            "identity": str(identity or ""),
            "start_at": now.isoformat(timespec="seconds"),
            "end_at": "",
            "duration_seconds": 0,
            "tx_bytes": 0,
            "rx_bytes": 0,
            "avg_bps": 0,
            "status": "aberta",
        }
        event = {
            "time": now.isoformat(timespec="seconds"),
            "connection_id": connection_id,
            "service": service,
            "peer": str(peer or "unknown"),
            "identity": str(identity or ""),
            "event": "Conexão iniciada",
            "tx_bytes": 0,
            "rx_bytes": 0,
        }
        _runtime_upsert_connection(record)
        _runtime_append_access_event(event)
        with self._lock:
            self._writers[id(writer)] = (writer, service)
            self._active_connections[id(writer)] = record
            self._access_counts["total"] = int(self._access_counts.get("total", 0)) + 1
            self._access_counts[service] = int(self._access_counts.get(service, 0)) + 1
            self._recent_accesses.insert(0, {
                "time_iso": event["time"],
                "time": _format_admin_datetime(event["time"]),
                "service": service,
                "peer": str(peer or "unknown"),
                "identity": str(identity or ""),
                "event": "Conexão iniciada",
                "tx_bytes": 0,
                "rx_bytes": 0,
            })
            self._recent_accesses = self._recent_accesses[:20]

    def unregister(self, writer: object, peer: str = "unknown") -> None:
        now = _runtime_now_local()
        event: Optional[dict] = None
        record: Optional[dict] = None
        with self._lock:
            item = self._writers.pop(id(writer), None)
            record = self._active_connections.pop(id(writer), None)
            if item is not None:
                service = item[1]
                if record is None:
                    record = {
                        "id": _runtime_new_connection_id(service),
                        "date": _runtime_log_date_key(now),
                        "service": service,
                        "peer": str(peer or "unknown"),
                        "identity": self._access_identities.get((service, peer), ""),
                        "start_at": now.isoformat(timespec="seconds"),
                        "tx_bytes": 0,
                        "rx_bytes": 0,
                    }
                start_dt = _runtime_parse_iso_local(record.get("start_at")) or now
                duration = max(0, int((now - start_dt).total_seconds()))
                total_bytes = int(record.get("tx_bytes") or 0) + int(record.get("rx_bytes") or 0)
                record["peer"] = str(peer or record.get("peer") or "unknown")
                record["identity"] = str(record.get("identity") or self._access_identities.get((service, peer), ""))
                record["end_at"] = now.isoformat(timespec="seconds")
                record["duration_seconds"] = duration
                record["avg_bps"] = int(total_bytes / duration) if duration > 0 else total_bytes
                record["status"] = "encerrada"
                event = {
                    "time": now.isoformat(timespec="seconds"),
                    "connection_id": str(record.get("id") or ""),
                    "service": service,
                    "peer": record["peer"],
                    "identity": record["identity"],
                    "event": "Conexão encerrada",
                    "tx_bytes": int(record.get("tx_bytes") or 0),
                    "rx_bytes": int(record.get("rx_bytes") or 0),
                    "duration_seconds": duration,
                    "avg_bps": int(record.get("avg_bps") or 0),
                }
                self._recent_accesses.insert(0, {
                    "time_iso": event["time"],
                    "time": _format_admin_datetime(event["time"]),
                    "service": service,
                    "peer": event["peer"],
                    "identity": event["identity"],
                    "event": "Conexão encerrada",
                    "tx_bytes": event["tx_bytes"],
                    "rx_bytes": event["rx_bytes"],
                })
                self._recent_accesses = self._recent_accesses[:20]
        if record:
            _runtime_upsert_connection(record)
        if event:
            _runtime_append_access_event(event)

    def update_access_identity(self, service: str, peer: str, identity: str) -> None:
        service = service if service in {"meganet", "telebradesco"} else "meganet"
        identity = str(identity or "").strip()
        if not identity:
            return
        connection_ids: list[str] = []
        with self._lock:
            self._access_identities[(service, peer)] = identity
            for record in self._active_connections.values():
                if record.get("service") == service and record.get("peer") == peer:
                    record["identity"] = identity
                    connection_ids.append(str(record.get("id") or ""))
            for item in self._recent_accesses:
                if item.get("service") == service and item.get("peer") == peer:
                    item["identity"] = identity
        for connection_id in connection_ids:
            _runtime_update_connection_identity(connection_id, service, peer, identity)

    def record_traffic(self, service: str, direction: str, size: int) -> None:
        self.record_traffic_for_writer(None, service, direction, size)

    def record_traffic_for_writer(self, writer: Optional[object], service: str, direction: str, size: int) -> None:
        if size <= 0:
            return
        service = service if service in {"meganet", "telebradesco"} else "meganet"
        direction = "tx" if direction == "tx" else "rx"
        now_text = _runtime_now_local().isoformat(timespec="seconds")
        changed_record: Optional[dict] = None
        with self._lock:
            # Rotate the in-memory daily state before applying the first delta
            # of a new day. Otherwise _ensure_bucket_locked() can replace
            # self._traffic after this increment and the dashboard loses the
            # first post-midnight transfer even though persistence is correct.
            self._ensure_current_day_locked()
            self._traffic[service][direction] += int(size)
            bucket = self._ensure_bucket_locked()
            bucket[f"{service}_{direction}"] += int(size)
            record = self._active_connections.get(id(writer)) if writer is not None else None
            if record is None:
                for candidate in self._active_connections.values():
                    if candidate.get("service") == service:
                        record = candidate
                        break
            if record is not None:
                record[f"{direction}_bytes"] = int(record.get(f"{direction}_bytes") or 0) + int(size)
                record["last_traffic_at"] = now_text
                changed_record = dict(record)
        _runtime_record_traffic_persistent(service, direction, int(size))
        if changed_record:
            _runtime_upsert_connection(changed_record)

    def connection_count(self, service: Optional[str] = None) -> int:
        with self._lock:
            if service in {"meganet", "telebradesco"}:
                return sum(1 for _writer, item_service in self._writers.values() if item_service == service)
            return len(self._writers)

    def dashboard_snapshot(self) -> dict:
        with self._lock:
            # O construtor carrega o histórico persistente e a rotação diária
            # recarrega o novo dia. Durante a vida do processo, a memória é a
            # fonte mais recente: reler o disco aqui poderia sobrescrever dados
            # que continuaram válidos em memória após uma falha temporária de I/O.
            self._ensure_current_day_locked()
            self._ensure_bucket_locked()
            recent = self._recent_accesses[:10]
            return {
                "connections": {
                    "total": len(self._writers),
                    "meganet": sum(1 for _writer, service in self._writers.values() if service == "meganet"),
                    "telebradesco": sum(1 for _writer, service in self._writers.values() if service == "telebradesco"),
                },
                "traffic": copy.deepcopy(self._traffic),
                "access_counts": copy.deepcopy(self._access_counts),
                "buckets": copy.deepcopy(self._traffic_buckets),
                "recent_accesses": copy.deepcopy(recent[:10]),
            }

    def request_close_all(self, reason: str) -> int:
        with self._lock:
            count = len(self._writers)
        if count:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.close_all(reason)))
        return count

    def request_close_service(self, service: str, reason: str) -> int:
        service = service if service in {"meganet", "telebradesco"} else "meganet"
        with self._lock:
            count = sum(1 for _writer, item_service in self._writers.values() if item_service == service)
        if count:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.close_service(service, reason)))
        return count

    async def close_service(self, service: str, reason: str) -> None:
        service = service if service in {"meganet", "telebradesco"} else "meganet"
        with self._lock:
            writers = [item[0] for item in self._writers.values() if item[1] == service]
        logging.info("SERVICE_RUNTIME_CLOSE_SERVICE service=%s sessions=%d reason=%s", service, len(writers), reason)
        for writer in writers:
            try:
                writer.close()
            except Exception:
                pass
        for writer in writers:
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def close_all(self, reason: str) -> None:
        with self._lock:
            writers = [item[0] for item in self._writers.values()]
        logging.info("SERVICE_RUNTIME_CLOSE_ALL sessions=%d reason=%s", len(writers), reason)
        for writer in writers:
            try:
                writer.close()
            except Exception:
                pass
        for writer in writers:
            try:
                await writer.wait_closed()
            except Exception:
                pass
