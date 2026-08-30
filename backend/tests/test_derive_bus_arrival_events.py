import gzip
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from db import get_connection
from scripts.derive_bus_arrival_events import process_day, run_derive

LOCAL_TZ = ZoneInfo("America/New_York")
SERVICE_DATE = date(2026, 8, 29)  # a real Saturday -> exercises "weekend" day_type too
T0 = datetime(2026, 8, 29, 18, 47, 20, tzinfo=timezone.utc)


def _record(
    polled_at: datetime,
    route_id="Q102",
    direction=0,
    stop_id="450154",
    vehicle_id="MTABC_9207",
    trip_id="46284506-LGPC6-LG_C6-Saturday-03",
    predicted_arrival_ts=None,
    raw_source="tripUpdates",
) -> dict:
    return {
        "polled_at": polled_at.isoformat(),
        "agency": "MTA",
        "route_id": route_id,
        "direction": direction,
        "stop_id": stop_id,
        "vehicle_id": vehicle_id,
        "trip_id": trip_id,
        "predicted_arrival_ts": predicted_arrival_ts,
        "distance_along_route": None,
        "raw_source": raw_source,
    }


def _write_ndjson(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _write_ndjson_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "risk.sqlite3"))
    yield connection
    connection.close()


def _all_events(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM arrival_events").fetchall()]


def test_clean_passage_computes_interpolated_midpoint_and_clean_quality(tmp_path, conn):
    # Poll 1: stop 450154 pending. Poll 2 (+60s): it's gone -> passed.
    records = [
        _record(T0, stop_id="450154", predicted_arrival_ts=1788029400),
        _record(T0, stop_id="450200", predicted_arrival_ts=1788029500),  # stays pending at EOF
        _record(T0 + timedelta(seconds=60), stop_id="450200", predicted_arrival_ts=1788029490),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    event = events[0]
    assert event["agency"] == "bus"
    assert event["route_id"] == "Q102"
    assert event["direction"] == "0"
    assert event["stop_id"] == "450154"
    assert event["vehicle_id"] == "MTABC_9207"
    assert event["trip_id"] == "46284506-LGPC6-LG_C6-Saturday-03"
    assert event["derivation_quality"] == "clean"
    assert event["scheduled_arrival_ts"] is None
    assert event["delay_seconds"] is None

    expected_midpoint = T0 + timedelta(seconds=30)
    assert datetime.fromisoformat(event["observed_arrival_ts"]) == expected_midpoint
    assert event["day_type"] == "weekend"  # SERVICE_DATE is a Saturday
    assert event["hour_bucket"] == expected_midpoint.astimezone(LOCAL_TZ).hour


def test_ambiguous_passage_over_90s_gap_still_emitted(tmp_path, conn):
    records = [
        _record(T0, stop_id="S1", vehicle_id="V2"),
        _record(T0 + timedelta(seconds=300), stop_id="S2", vehicle_id="V2"),  # 5 min later, S1 gone
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    # S1's passage is ambiguous; S2 is still pending at EOF -> dropped.
    assert len(events) == 1
    event = events[0]
    assert event["stop_id"] == "S1"
    assert event["derivation_quality"] == "ambiguous"
    expected_midpoint = T0 + timedelta(seconds=150)
    assert datetime.fromisoformat(event["observed_arrival_ts"]) == expected_midpoint


def test_predicted_arrival_ts_at_t_minus_5_picked_within_tolerance(tmp_path, conn):
    # Polls every 60s from T0 to T0+360s (last_seen), then passage at T0+390s (gap 30s, clean).
    # observed_arrival_ts = midpoint(T0+360, T0+390) = T0+375.
    # T-5 target = T0+375 - 300 = T0+75. Closest poll is T0+60 (diff 15s, within 90s tol).
    vehicle_id = "V3"
    stop_id = "S1"
    poll_offsets_and_predictions = [
        (0, 100), (60, 200), (120, 300), (180, 400), (240, 500), (300, 600), (360, 700),
    ]
    records = [
        _record(T0 + timedelta(seconds=off), stop_id=stop_id, vehicle_id=vehicle_id, predicted_arrival_ts=pred)
        for off, pred in poll_offsets_and_predictions
    ]
    # passage poll: stop disappears, some other stop appears to keep the vehicle present
    records.append(_record(T0 + timedelta(seconds=390), stop_id="OTHER", vehicle_id=vehicle_id))
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    event = events[0]
    assert event["stop_id"] == stop_id
    assert event["derivation_quality"] == "clean"
    expected_predicted_epoch = 200  # from the T0+60 poll
    assert datetime.fromisoformat(event["predicted_arrival_ts_at_T_minus_5"]) == datetime.fromtimestamp(
        expected_predicted_epoch, tz=timezone.utc
    )


def test_predicted_arrival_ts_at_t_minus_5_null_when_no_poll_within_tolerance(tmp_path, conn):
    # Only one poll in history, far from the T-5 target -> NULL, never a stale pick.
    vehicle_id = "V4"
    stop_id = "S1"
    records = [
        _record(T0, stop_id=stop_id, vehicle_id=vehicle_id, predicted_arrival_ts=111),
        _record(T0 + timedelta(seconds=30), stop_id="OTHER", vehicle_id=vehicle_id),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["predicted_arrival_ts_at_T_minus_5"] is None


def test_predicted_arrival_ts_at_t_minus_5_null_when_closest_poll_had_no_prediction(tmp_path, conn):
    # Same shape as the "picked" test, but the poll closest to the T-5
    # target itself had no prediction (null) that cycle -- per the brief,
    # this yields NULL too, not a fallback to some other poll.
    vehicle_id = "V3b"
    stop_id = "S1"
    poll_offsets_and_predictions = [
        (0, 100), (60, None), (120, 300), (180, 400), (240, 500), (300, 600), (360, 700),
    ]
    records = [
        _record(T0 + timedelta(seconds=off), stop_id=stop_id, vehicle_id=vehicle_id, predicted_arrival_ts=pred)
        for off, pred in poll_offsets_and_predictions
    ]
    records.append(_record(T0 + timedelta(seconds=390), stop_id="OTHER", vehicle_id=vehicle_id))
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["predicted_arrival_ts_at_T_minus_5"] is None


def test_trip_id_change_mid_file_preserves_old_trip_identity(tmp_path, conn):
    vehicle_id = "V5"
    records = [
        # Old trip: vehicle pending at stop S1.
        _record(
            T0, stop_id="S1", vehicle_id=vehicle_id,
            route_id="OLDROUTE", direction=0, trip_id="TRIP_A_OLD",
        ),
        # New poll: vehicle has switched to a new trip entirely -- S1 (old
        # trip) is gone, S9 (new trip) appears. The vehicle completed its
        # old trip and started a new one; this is the normal case, not a
        # special one.
        _record(
            T0 + timedelta(seconds=60), stop_id="S9", vehicle_id=vehicle_id,
            route_id="NEWROUTE", direction=1, trip_id="TRIP_B_NEW",
        ),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    # S1 resolves (old trip); S9 is left pending at EOF (dropped).
    assert len(events) == 1
    event = events[0]
    assert event["stop_id"] == "S1"
    assert event["route_id"] == "OLDROUTE"
    assert event["direction"] == "0"
    assert event["trip_id"] == "TRIP_A_OLD"
    assert event["derivation_quality"] == "clean"


def test_stop_still_pending_at_eof_is_dropped(tmp_path, conn):
    records = [
        _record(T0, stop_id="S1", vehicle_id="V6"),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    assert _all_events(conn) == []


def test_vehicle_positions_lines_ignored(tmp_path, conn):
    records = [
        _record(T0, stop_id="S1", vehicle_id="V7"),
        {
            "polled_at": T0.isoformat(),
            "agency": "MTA",
            "route_id": "Q102",
            "direction": 0,
            "stop_id": "S1",
            "vehicle_id": "V7",
            "trip_id": "SOME_TRIP",
            "predicted_arrival_ts": None,
            "distance_along_route": None,
            "raw_source": "vehiclePositions",
            "lat": 40.7,
            "lon": -73.9,
        },
        _record(T0 + timedelta(seconds=30), stop_id="S2", vehicle_id="V7"),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["stop_id"] == "S1"
    assert events[0]["derivation_quality"] == "clean"


def test_gzip_input_is_read_correctly(tmp_path, conn):
    records = [
        _record(T0, stop_id="S1", vehicle_id="V8"),
        _record(T0 + timedelta(seconds=45), stop_id="S2", vehicle_id="V8"),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson.gz"
    _write_ndjson_gz(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["stop_id"] == "S1"
    assert events[0]["derivation_quality"] == "clean"


def test_rerunning_already_ingested_day_is_a_noop(tmp_path, conn):
    records = [
        _record(T0, stop_id="S1", vehicle_id="V9"),
        _record(T0 + timedelta(seconds=30), stop_id="S2", vehicle_id="V9"),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)
    assert len(_all_events(conn)) == 1

    process_day(conn, raw_path, SERVICE_DATE)
    assert len(_all_events(conn)) == 1


def test_trip_update_line_missing_required_field_is_skipped_not_crashed(tmp_path, conn, capsys):
    # Final whole-branch review, Minor #4: a parseable-JSON tripUpdates line
    # missing a field this module directly indexes (e.g. "route_id") must
    # be skipped with a logged warning, like a malformed-JSON line already
    # is -- not crash the whole day via a raw KeyError.
    good_record = _record(T0, stop_id="S1", vehicle_id="V10")
    bad_record = _record(T0 + timedelta(seconds=30), stop_id="S2", vehicle_id="V10")
    del bad_record["route_id"]
    records = [
        good_record,
        bad_record,
        _record(T0 + timedelta(seconds=60), stop_id="S3", vehicle_id="V10"),
    ]
    raw_path = tmp_path / f"{SERVICE_DATE.isoformat()}.ndjson"
    _write_ndjson(raw_path, records)

    process_day(conn, raw_path, SERVICE_DATE)

    # The bad line drops out entirely (never marks S2 as tracked), so S1's
    # passage is observed at the S3 poll, not the (skipped) S2 one.
    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["stop_id"] == "S1"
    assert "missing field" in capsys.readouterr().err


def test_run_derive_processes_both_ndjson_and_gz_files_in_directory(tmp_path, conn):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    day1 = date(2026, 8, 24)  # Monday -> weekday
    day2 = date(2026, 8, 29)  # Saturday -> weekend

    _write_ndjson(
        raw_dir / f"{day1.isoformat()}.ndjson",
        [
            _record(T0, stop_id="S1", vehicle_id="VA"),
            _record(T0 + timedelta(seconds=30), stop_id="S2", vehicle_id="VA"),
        ],
    )
    _write_ndjson_gz(
        raw_dir / f"{day2.isoformat()}.ndjson.gz",
        [
            _record(T0, stop_id="S1", vehicle_id="VB"),
            _record(T0 + timedelta(seconds=30), stop_id="S2", vehicle_id="VB"),
        ],
    )

    run_derive(raw_dir, conn)

    events = _all_events(conn)
    assert len(events) == 2
    day_types = {e["service_date"]: e["day_type"] for e in events}
    assert day_types[day1.isoformat()] == "weekday"
    assert day_types[day2.isoformat()] == "weekend"
