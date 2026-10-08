from __future__ import annotations

import asyncio
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from meganet_server.runtime import statistics as rs


class _Writer:
    pass


def _use_log_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    access = tmp_path / "access"
    traffic = tmp_path / "traffic"
    access.mkdir()
    traffic.mkdir()
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_ACCESS_LOG_DIR", access)
    monkeypatch.setattr(rs, "DEFAULT_RUNTIME_TRAFFIC_LOG_DIR", traffic)
    return access, traffic


def _set_clock(monkeypatch, current: list[dt.datetime]) -> None:
    monkeypatch.setattr(rs, "_runtime_now_local", lambda: current[0])
    monkeypatch.setattr(
        rs.UnifiedServiceRuntime,
        "_minute_key",
        staticmethod(lambda now=None: (now or current[0]).strftime("%Y-%m-%d %H:%M")),
    )


def test_runtime_daily_files_rotate_on_date_change(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 23, 59, 58)]
    _set_clock(monkeypatch, current)

    rs._runtime_append_access_event({"service": "meganet", "event": "d1"})
    rs._runtime_record_traffic_persistent("meganet", "tx", 10)
    current[0] = dt.datetime(2026, 10, 5, 0, 0, 2)
    rs._runtime_append_access_event({"service": "telebradesco", "event": "d2"})
    rs._runtime_record_traffic_persistent("telebradesco", "rx", 20)

    assert (access / "access_20261004.json").exists()
    assert (access / "access_20261005.json").exists()
    assert (traffic / "traffic_20261004.json").exists()
    assert (traffic / "traffic_20261005.json").exists()
    assert json.loads((traffic / "traffic_20261004.json").read_text(encoding="utf-8"))["totals"]["meganet"]["tx"] == 10
    assert json.loads((traffic / "traffic_20261005.json").read_text(encoding="utf-8"))["totals"]["telebradesco"]["rx"] == 20


def test_runtime_dashboard_drops_previous_day_totals_after_midnight_without_new_traffic(tmp_path, monkeypatch) -> None:
    _access, _traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 23, 59, 30)]
    _set_clock(monkeypatch, current)

    rs._runtime_record_traffic_persistent("meganet", "tx", 100)
    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    assert runtime.dashboard_snapshot()["traffic"]["meganet"]["tx"] == 100

    current[0] = dt.datetime(2026, 10, 5, 0, 0, 5)
    snapshot = runtime.dashboard_snapshot()
    assert snapshot["traffic"] == {
        "meganet": {"tx": 0, "rx": 0},
        "telebradesco": {"tx": 0, "rx": 0},
    }
    assert all(str(bucket.get("key", "")).startswith("2026-10-05 ") for bucket in snapshot["buckets"])


def test_runtime_session_crossing_midnight_keeps_single_connection_and_rotates_events_and_traffic(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 23, 59, 58)]
    _set_clock(monkeypatch, current)

    runtime = rs.UnifiedServiceRuntime(asyncio.new_event_loop())
    writer = _Writer()
    runtime.register(writer, "meganet", "10.0.0.1:1000")
    runtime.update_access_identity("meganet", "10.0.0.1:1000", "KAVERA")
    runtime.record_traffic_for_writer(writer, "meganet", "rx", 10)

    current[0] = dt.datetime(2026, 10, 5, 0, 0, 3)
    runtime.record_traffic_for_writer(writer, "meganet", "tx", 20)
    runtime.unregister(writer, "10.0.0.1:1000")

    day1 = json.loads((access / "access_20261004.json").read_text(encoding="utf-8"))
    day2 = json.loads((access / "access_20261005.json").read_text(encoding="utf-8"))
    assert len(day1["connections"]) == 1
    connection = day1["connections"][0]
    assert connection["start_at"] == "2026-10-04T23:59:58"
    assert connection["end_at"] == "2026-10-05T00:00:03"
    assert connection["status"] == "encerrada"
    assert connection["identity"] == "KAVERA"
    assert connection["rx_bytes"] == 10
    assert connection["tx_bytes"] == 20
    assert [item["event"] for item in day1["events"]] == ["Conexão iniciada"]
    assert [item["event"] for item in day2["events"]] == ["Conexão encerrada"]

    assert json.loads((traffic / "traffic_20261004.json").read_text(encoding="utf-8"))["totals"]["meganet"] == {"tx": 0, "rx": 10}
    assert json.loads((traffic / "traffic_20261005.json").read_text(encoding="utf-8"))["totals"]["meganet"] == {"tx": 20, "rx": 0}

    stats = rs._runtime_query_statistics({
        "date_from": ["2026-10-04"],
        "date_to": ["2026-10-05"],
        "service": ["meganet"],
        "user": ["kavera"],
    })
    assert len(stats["connections"]) == 1
    assert len(stats["events"]) == 2
    assert stats["totals"]["meganet"] == {"tx": 20, "rx": 10}


def test_runtime_concurrent_writes_do_not_lose_events_or_traffic(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 12, 34, 56)]
    _set_clock(monkeypatch, current)

    total = 64

    def write_one(index: int) -> None:
        service = "meganet" if index % 2 == 0 else "telebradesco"
        rs._runtime_append_access_event({
            "connection_id": f"c{index}",
            "service": service,
            "identity": f"user{index}",
            "event": "simultaneous",
            "tx_bytes": index,
            "rx_bytes": index + 1,
        })
        rs._runtime_record_traffic_persistent(service, "tx", 1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_one, range(total)))

    access_payload = json.loads((access / "access_20261004.json").read_text(encoding="utf-8"))
    traffic_payload = json.loads((traffic / "traffic_20261004.json").read_text(encoding="utf-8"))
    assert len(access_payload["events"]) == total
    assert {item["connection_id"] for item in access_payload["events"]} == {f"c{i}" for i in range(total)}
    assert traffic_payload["totals"]["meganet"]["tx"] == total // 2
    assert traffic_payload["totals"]["telebradesco"]["tx"] == total // 2


def test_runtime_statistics_filters_are_case_insensitive_and_normalize_reversed_dates(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    (traffic / "traffic_20261003.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": "20261003",
        "totals": {},
        "buckets": [{"key": "2026-10-03 09:00", "label": "09:00", "meganet_tx": 11, "meganet_rx": 12, "telebradesco_tx": 0, "telebradesco_rx": 0}],
        "updated_at": "",
    }), encoding="utf-8")
    (traffic / "traffic_20261004.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": "20261004",
        "totals": {},
        "buckets": [{"key": "2026-10-04 10:00", "label": "10:00", "meganet_tx": 0, "meganet_rx": 0, "telebradesco_tx": 21, "telebradesco_rx": 22}],
        "updated_at": "",
    }), encoding="utf-8")
    (access / "access_20261003.json").write_text(json.dumps({
        "schema": "runtime_access_daily_v1",
        "date": "20261003",
        "connections": [{"id": "m1", "service": "meganet", "peer": "HOST-A:1", "identity": "Kavera", "start_at": "2026-10-03T09:00:00", "end_at": "2026-10-03T09:01:00", "tx_bytes": 11, "rx_bytes": 12, "duration_seconds": 60}],
        "events": [],
        "updated_at": "",
    }), encoding="utf-8")
    (access / "access_20261004.json").write_text(json.dumps({
        "schema": "runtime_access_daily_v1",
        "date": "20261004",
        "connections": [{"id": "t1", "service": "telebradesco", "peer": "HOST-B:2", "identity": "2924-0023456-7", "start_at": "2026-10-04T10:00:00", "end_at": "2026-10-04T10:01:00", "tx_bytes": 21, "rx_bytes": 22, "duration_seconds": 60}],
        "events": [],
        "updated_at": "",
    }), encoding="utf-8")

    stats = rs._runtime_query_statistics({
        "date_from": ["2026-10-04"],
        "date_to": ["2026-10-03"],
        "service": ["MEGANET"],
        "origin": ["host-a"],
        "user": ["KAV"],
    })
    assert stats["start_date"] == dt.date(2026, 10, 3)
    assert stats["end_date"] == dt.date(2026, 10, 4)
    assert [item["id"] for item in stats["connections"]] == ["m1"]
    assert stats["totals"]["meganet"] == {"tx": 11, "rx": 12}
    assert stats["totals"]["telebradesco"] == {"tx": 0, "rx": 0}


def test_runtime_statistics_tolerates_partially_corrupt_records(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    current = [dt.datetime(2026, 10, 4, 12, 0, 0)]
    _set_clock(monkeypatch, current)

    (traffic / "traffic_20261004.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": "20261004",
        "totals": {
            "meganet": {"tx": "broken", "rx": None},
            "telebradesco": {"tx": 12, "rx": "34"},
        },
        "buckets": [
            {"key": "2026-10-04 11:59", "label": "11:59", "meganet_tx": "bad", "meganet_rx": None, "telebradesco_tx": "12", "telebradesco_rx": 34},
            "not-a-dict",
        ],
        "updated_at": "",
    }), encoding="utf-8")
    (access / "access_20261004.json").write_text(json.dumps({
        "schema": "runtime_access_daily_v1",
        "date": "20261004",
        "connections": [
            {"id": "ok", "service": "meganet", "peer": "p", "identity": "u", "start_at": "2026-10-04T11:59:00", "end_at": "", "tx_bytes": "bad", "rx_bytes": None, "duration_seconds": "bad", "avg_bps": "bad"},
            "not-a-dict",
        ],
        "events": [
            {"time": "2026-10-04T11:59:30", "service": "meganet", "peer": "p", "identity": "u", "event": "Acesso", "tx_bytes": "bad", "rx_bytes": None},
            123,
        ],
        "updated_at": "",
    }), encoding="utf-8")

    totals, buckets = rs._runtime_load_today_traffic()
    assert totals == {"meganet": {"tx": 0, "rx": 0}, "telebradesco": {"tx": 12, "rx": 34}}
    assert buckets == [{
        "key": "2026-10-04 11:59",
        "label": "11:59",
        "meganet_tx": 0,
        "meganet_rx": 0,
        "telebradesco_tx": 12,
        "telebradesco_rx": 34,
    }]
    recent = rs._runtime_load_recent_accesses()
    assert len(recent) == 1
    assert recent[0]["tx_bytes"] == 0
    assert recent[0]["rx_bytes"] == 0

    stats = rs._runtime_query_statistics({"date_from": ["2026-10-04"], "date_to": ["2026-10-04"]})
    assert stats["totals"]["meganet"] == {"tx": 0, "rx": 0}
    assert stats["totals"]["telebradesco"] == {"tx": 12, "rx": 34}
    assert stats["connections"][0]["tx_bytes"] == 0
    assert stats["connections"][0]["rx_bytes"] == 0
    assert stats["connections"][0]["duration_seconds"] == 0
    assert stats["connections"][0]["avg_bps"] == 0
    assert stats["events"][0]["tx_bytes"] == 0
    assert stats["events"][0]["rx_bytes"] == 0


def test_runtime_large_history_and_aggregate_consistency(tmp_path, monkeypatch) -> None:
    access, traffic = _use_log_dirs(monkeypatch, tmp_path)
    date_key = "20261004"
    date_text = "2026-10-04"
    count = 2500
    connections = []
    events = []
    mega_tx = mega_rx = tele_tx = tele_rx = 0
    for index in range(count):
        service = "meganet" if index % 2 == 0 else "telebradesco"
        tx = index % 17 + 1
        rx = index % 13 + 1
        if service == "meganet":
            mega_tx += tx
            mega_rx += rx
        else:
            tele_tx += tx
            tele_rx += rx
        second = index % 60
        minute = (index // 60) % 60
        hour = 8 + ((index // 3600) % 8)
        stamp = f"{date_text}T{hour:02d}:{minute:02d}:{second:02d}"
        connections.append({
            "id": f"c{index:04d}",
            "service": service,
            "peer": f"10.0.0.{index % 255}:{1000 + index}",
            "identity": f"user-{index:04d}",
            "start_at": stamp,
            "end_at": stamp,
            "duration_seconds": 0,
            "tx_bytes": tx,
            "rx_bytes": rx,
            "avg_bps": tx + rx,
            "status": "encerrada",
        })
        events.append({"time": stamp, "connection_id": f"c{index:04d}", "service": service, "peer": "p", "identity": f"user-{index:04d}", "event": "Conexão encerrada", "tx_bytes": tx, "rx_bytes": rx})

    (access / f"access_{date_key}.json").write_text(json.dumps({
        "schema": "runtime_access_daily_v1",
        "date": date_key,
        "connections": connections,
        "events": events,
        "updated_at": "",
    }), encoding="utf-8")
    (traffic / f"traffic_{date_key}.json").write_text(json.dumps({
        "schema": "runtime_traffic_daily_v1",
        "date": date_key,
        "totals": {
            "meganet": {"tx": mega_tx, "rx": mega_rx},
            "telebradesco": {"tx": tele_tx, "rx": tele_rx},
        },
        "buckets": [{
            "key": f"{date_text} 08:00",
            "label": "08:00",
            "meganet_tx": mega_tx,
            "meganet_rx": mega_rx,
            "telebradesco_tx": tele_tx,
            "telebradesco_rx": tele_rx,
        }],
        "updated_at": "",
    }), encoding="utf-8")

    stats = rs._runtime_query_statistics({"date_from": [date_text], "date_to": [date_text]})
    assert len(stats["connections"]) == count
    assert len(stats["events"]) == count
    assert stats["connections"][0]["start_at"] >= stats["connections"][-1]["start_at"]

    by_service = {
        "meganet": {
            "tx": sum(int(item["tx_bytes"]) for item in stats["connections"] if item["service"] == "meganet"),
            "rx": sum(int(item["rx_bytes"]) for item in stats["connections"] if item["service"] == "meganet"),
        },
        "telebradesco": {
            "tx": sum(int(item["tx_bytes"]) for item in stats["connections"] if item["service"] == "telebradesco"),
            "rx": sum(int(item["rx_bytes"]) for item in stats["connections"] if item["service"] == "telebradesco"),
        },
    }
    assert stats["totals"] == by_service

    one_user = rs._runtime_query_statistics({
        "date_from": [date_text],
        "date_to": [date_text],
        "service": ["telebradesco"],
        "user": ["user-2499"],
    })
    assert [item["id"] for item in one_user["connections"]] == ["c2499"]
