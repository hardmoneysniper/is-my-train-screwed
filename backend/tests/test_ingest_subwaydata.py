import csv
import io
import tarfile
import zipfile
from datetime import date, timezone
from pathlib import Path

import pytest

from app.realtime_proxy import TripIndex
from db import get_connection
from scripts.ingest_subwaydata import (
    build_static_stop_times_index,
    day_type_for,
    process_day,
    run_ingest,
)

TRIPS_FIELDS = [
    "trip_uid", "trip_id", "route_id", "direction_id", "start_time",
    "vehicle_id", "last_observed", "marked_past", "num_updates",
    "num_schedule_changes", "num_schedule_rewrites",
]
STOP_TIMES_FIELDS = [
    "trip_uid", "stop_id", "track", "arrival_time", "departure_time",
    "last_observed", "marked_past",
]

# RT-style trip_id as it appears in subwaydata.nyc's trips.csv (matches the
# brief's real sample row format: "024000_GS.N01R").
RT_TRIP_ID = "024000_GS.N01R"
# The corresponding static trip_id -- the RT id is the static id with
# everything up to and including the first underscore stripped, per
# TripIndex/realtime_proxy.py's documented format.
STATIC_TRIP_ID = "SCHEDULE01_024000_GS.N01R"

WEEKDAY_DATE = date(2026, 8, 25)  # a real Tuesday
WEEKEND_DATE = date(2026, 8, 29)  # a real Saturday

# Scheduled 08:00:00 local (America/New_York, EDT) on 2026-08-25 ==
# 2026-08-25T12:00:00Z == epoch 1787659200 (verified independently via
# zoneinfo during test authoring). Observed 30s late.
SCHEDULED_EPOCH_0800_LOCAL = 1787659200
OBSERVED_EPOCH_30S_LATE = SCHEDULED_EPOCH_0800_LOCAL + 30


def _write_csv(rows: list[dict], fieldnames: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _add_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _make_tar(tmp_path: Path, date_str: str, trips_rows: list[dict], stop_times_rows: list[dict]) -> Path:
    tar_path = tmp_path / f"{date_str}.tar.xz"
    with tarfile.open(tar_path, "w:xz") as tar:
        _add_member(tar, f"subwaydatanyc_{date_str}_trips.csv", _write_csv(trips_rows, TRIPS_FIELDS))
        _add_member(
            tar, f"subwaydatanyc_{date_str}_stop_times.csv",
            _write_csv(stop_times_rows, STOP_TIMES_FIELDS),
        )
    return tar_path


def _trip_row(trip_uid="TUID1", trip_id=RT_TRIP_ID, route_id="GS", direction_id="0", vehicle_id="0S 0400  GCS/TSS"):
    return {
        "trip_uid": trip_uid,
        "trip_id": trip_id,
        "route_id": route_id,
        "direction_id": direction_id,
        "start_time": "1787198400",
        "vehicle_id": vehicle_id,
        "last_observed": "1787212866",
        "marked_past": "1787212869",
        "num_updates": "741",
        "num_schedule_changes": "0",
        "num_schedule_rewrites": "0",
    }


def _stop_time_row(trip_uid="TUID1", stop_id="901N", arrival_time="", departure_time=""):
    return {
        "trip_uid": trip_uid,
        "stop_id": stop_id,
        "track": "4",
        "arrival_time": arrival_time,
        "departure_time": departure_time,
        "last_observed": "1787212786",
        "marked_past": "1787212788",
    }


def _make_static_gtfs_zip(tmp_path: Path) -> Path:
    """A tiny synthetic static GTFS zip -- never touches the real 150MB
    subway.zip. One trip whose RT-style suffix is RT_TRIP_ID, one stop
    (901N) with a normal time, one stop (902N) with an extended (>24:00:00)
    time to exercise that parsing path."""
    zip_path = tmp_path / "synthetic_subway.zip"
    trips_txt = (
        "route_id,service_id,trip_id,trip_headsign,direction_id\n"
        f"GS,WEEKDAY,{STATIC_TRIP_ID},Grand Central,0\n"
    )
    stop_times_txt = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        f"{STATIC_TRIP_ID},08:00:00,08:00:00,901N,1\n"
        f"{STATIC_TRIP_ID},25:30:00,25:30:00,902N,2\n"
    )
    calendar_txt = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WEEKDAY,1,1,1,1,1,0,0,20250101,20261231\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("trips.txt", trips_txt)
        z.writestr("stop_times.txt", stop_times_txt)
        z.writestr("calendar.txt", calendar_txt)
    return zip_path


@pytest.fixture
def static_gtfs_zip(tmp_path):
    return _make_static_gtfs_zip(tmp_path)


@pytest.fixture
def trip_index(static_gtfs_zip):
    return TripIndex(static_gtfs_zip)


@pytest.fixture
def static_index(static_gtfs_zip):
    return build_static_stop_times_index(static_gtfs_zip)


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "risk.sqlite3"))
    yield connection
    connection.close()


def _all_events(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM arrival_events").fetchall()]


def test_matched_trip_and_stop_computes_scheduled_ts_and_delay(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    events = _all_events(conn)
    assert len(events) == 1
    event = events[0]
    assert event["agency"] == "subway"
    assert event["route_id"] == "GS"
    assert event["direction"] == "0"
    assert event["stop_id"] == "901N"
    assert event["vehicle_id"] == "0S 0400  GCS/TSS"
    assert event["trip_id"] == STATIC_TRIP_ID  # resolved static id, not the raw RT id
    assert event["delay_seconds"] == 30
    assert event["scheduled_arrival_ts"] is not None
    assert event["predicted_arrival_ts_at_T_minus_5"] is None
    assert event["derivation_quality"] == "clean"
    assert event["day_type"] == "weekday"
    assert event["hour_bucket"] == 8  # 08:00:30 local


def test_extended_time_past_midnight_parses_correctly(tmp_path, conn, trip_index, static_index):
    # 902N's static arrival is 25:30:00 on the service date -> 01:30:00
    # local the *next* calendar day. Observe right on time (delay ~0).
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    scheduled_local = datetime(2026, 8, 25, 0, 0, 0, tzinfo=tz) + timedelta(hours=25, minutes=30)
    observed_epoch = int(scheduled_local.astimezone(timezone.utc).timestamp())

    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(stop_id="902N", arrival_time=str(observed_epoch))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["delay_seconds"] == 0
    # the scheduled ts should have rolled into the next UTC calendar day
    assert events[0]["scheduled_arrival_ts"].startswith("2026-08-26")


def test_unmatchable_trip_id_is_skipped_entirely(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row(trip_id="999999_ZZ..X99R")],
        stop_times_rows=[_stop_time_row(arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    assert _all_events(conn) == []


def test_empty_arrival_and_departure_time_is_skipped(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(arrival_time="", departure_time="")],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    assert _all_events(conn) == []


def test_empty_arrival_time_falls_back_to_departure_time(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(arrival_time="", departure_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["delay_seconds"] == 30


def test_trip_uid_not_found_in_trips_dict_is_skipped(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row(trip_uid="TUID1")],
        stop_times_rows=[_stop_time_row(trip_uid="NO_SUCH_UID", arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    assert _all_events(conn) == []


def test_matched_trip_but_unmatched_stop_writes_row_with_null_delay(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(stop_id="NOT_IN_STATIC", arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)

    events = _all_events(conn)
    assert len(events) == 1
    assert events[0]["scheduled_arrival_ts"] is None
    assert events[0]["delay_seconds"] is None


def test_day_type_classification():
    assert day_type_for(WEEKDAY_DATE) == "weekday"
    assert day_type_for(WEEKEND_DATE) == "weekend"


def test_day_type_classification_recognizes_hardcoded_holiday():
    assert day_type_for(date(2026, 12, 25)) == "weekend"  # Christmas Day, a Friday


def test_rerunning_already_ingested_day_is_a_noop(tmp_path, conn, trip_index, static_index):
    date_str = WEEKDAY_DATE.isoformat()
    tar_path = _make_tar(
        tmp_path, date_str,
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)
    assert len(_all_events(conn)) == 1

    # Second run for the same day must not add a second row (whole-day
    # idempotent skip, not a per-row unique constraint).
    process_day(conn, tar_path, WEEKDAY_DATE, trip_index, static_index)
    assert len(_all_events(conn)) == 1


def test_run_ingest_processes_all_tar_files_in_directory(tmp_path, conn, trip_index, static_index):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _make_tar(
        raw_dir, WEEKDAY_DATE.isoformat(),
        trips_rows=[_trip_row()],
        stop_times_rows=[_stop_time_row(arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )
    _make_tar(
        raw_dir, WEEKEND_DATE.isoformat(),
        trips_rows=[_trip_row(trip_uid="TUID2")],
        stop_times_rows=[_stop_time_row(trip_uid="TUID2", arrival_time=str(OBSERVED_EPOCH_30S_LATE))],
    )

    run_ingest(raw_dir, conn, trip_index, static_index)

    events = _all_events(conn)
    assert len(events) == 2
    day_types = {e["service_date"]: e["day_type"] for e in events}
    assert day_types[WEEKDAY_DATE.isoformat()] == "weekday"
    assert day_types[WEEKEND_DATE.isoformat()] == "weekend"
