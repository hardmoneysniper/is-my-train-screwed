import csv
import io
import zipfile

import pytest

from scripts.filter_gtfs_by_route import filter_gtfs_by_route

# Synthetic multi-route GTFS fixture:
#   R1 (kept) has trips T1 (service S1, shape SH1) and T2 (service S2, shape SH1)
#   R2 (dropped) has trip T3 (service S3, shape SH2)
#   T1's stop_times reference a platform stop (STOP_PLATFORM_A) whose
#   parent_station is STATION_A -- STATION_A itself never appears in
#   stop_times, only as a parent, so it exercises the one-level-up walk.
#   STOP_UNUSED is never referenced by any stop_time and must be dropped.
#   S4 appears only in calendar_dates.txt (no calendar.txt row) and is not
#   used by any kept trip, exercising independent calendar_dates filtering.
_FILES = {
    "agency.txt": (
        ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        [
            {
                "agency_id": "MTABC",
                "agency_name": "MTA Bus Company",
                "agency_url": "http://example.com",
                "agency_timezone": "America/New_York",
            }
        ],
    ),
    "routes.txt": (
        ["route_id", "route_short_name", "route_type"],
        [
            {"route_id": "R1", "route_short_name": "Q70+", "route_type": "3"},
            {"route_id": "R2", "route_short_name": "M60+", "route_type": "3"},
        ],
    ),
    "trips.txt": (
        ["route_id", "trip_id", "service_id", "shape_id"],
        [
            {"route_id": "R1", "trip_id": "T1", "service_id": "S1", "shape_id": "SH1"},
            {"route_id": "R1", "trip_id": "T2", "service_id": "S2", "shape_id": "SH1"},
            {"route_id": "R2", "trip_id": "T3", "service_id": "S3", "shape_id": "SH2"},
        ],
    ),
    "stop_times.txt": (
        ["trip_id", "stop_id", "stop_sequence"],
        [
            {"trip_id": "T1", "stop_id": "STOP_PLATFORM_A", "stop_sequence": "1"},
            {"trip_id": "T1", "stop_id": "STOP_B", "stop_sequence": "2"},
            {"trip_id": "T2", "stop_id": "STOP_C", "stop_sequence": "1"},
            {"trip_id": "T3", "stop_id": "STOP_D", "stop_sequence": "1"},
        ],
    ),
    "stops.txt": (
        ["stop_id", "stop_name", "parent_station"],
        [
            {"stop_id": "STOP_PLATFORM_A", "stop_name": "Platform A", "parent_station": "STATION_A"},
            {"stop_id": "STATION_A", "stop_name": "Station A", "parent_station": ""},
            {"stop_id": "STOP_B", "stop_name": "Stop B", "parent_station": ""},
            {"stop_id": "STOP_C", "stop_name": "Stop C", "parent_station": ""},
            {"stop_id": "STOP_D", "stop_name": "Stop D", "parent_station": ""},
            {"stop_id": "STOP_UNUSED", "stop_name": "Unused", "parent_station": ""},
        ],
    ),
    "calendar.txt": (
        ["service_id", "monday", "start_date", "end_date"],
        [
            {"service_id": "S1", "monday": "1", "start_date": "20260101", "end_date": "20261231"},
            {"service_id": "S2", "monday": "1", "start_date": "20260101", "end_date": "20261231"},
            {"service_id": "S3", "monday": "1", "start_date": "20260101", "end_date": "20261231"},
        ],
    ),
    "calendar_dates.txt": (
        ["service_id", "date", "exception_type"],
        [
            {"service_id": "S1", "date": "20260704", "exception_type": "2"},
            {"service_id": "S3", "date": "20260704", "exception_type": "2"},
            {"service_id": "S4", "date": "20260704", "exception_type": "1"},
        ],
    ),
    "shapes.txt": (
        ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
        [
            {"shape_id": "SH1", "shape_pt_lat": "40.75", "shape_pt_lon": "-73.9", "shape_pt_sequence": "1"},
            {"shape_id": "SH2", "shape_pt_lat": "40.76", "shape_pt_lon": "-73.8", "shape_pt_sequence": "1"},
        ],
    ),
    "feed_info.txt": (
        ["feed_publisher_name", "feed_publisher_url", "feed_lang"],
        [{"feed_publisher_name": "MTA", "feed_publisher_url": "http://example.com", "feed_lang": "en"}],
    ),
}


def _write_fixture_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        for filename, (fieldnames, rows) in _FILES.items():
            buf = io.StringIO(newline="")
            writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(filename, buf.getvalue())
    return path


def _read_output_rows(output_zip, filename):
    with zipfile.ZipFile(output_zip, "r") as zf:
        with zf.open(filename) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
            return list(reader.fieldnames or []), list(reader)


@pytest.fixture
def source_zip(tmp_path):
    return _write_fixture_zip(tmp_path / "source.zip")


def test_only_requested_routes_survive(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    fieldnames, rows = _read_output_rows(output_zip, "routes.txt")
    assert fieldnames == _FILES["routes.txt"][0]
    assert [r["route_id"] for r in rows] == ["R1"]


def test_only_kept_trips_and_their_stop_times_survive(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    _, trips = _read_output_rows(output_zip, "trips.txt")
    assert {t["trip_id"] for t in trips} == {"T1", "T2"}

    _, stop_times = _read_output_rows(output_zip, "stop_times.txt")
    assert {st["trip_id"] for st in stop_times} == {"T1", "T2"}
    assert {st["stop_id"] for st in stop_times} == {"STOP_PLATFORM_A", "STOP_B", "STOP_C"}


def test_stops_include_parent_station_one_level_up(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    _, stops = _read_output_rows(output_zip, "stops.txt")
    stop_ids = {s["stop_id"] for s in stops}
    # Directly referenced stops plus the parent station of STOP_PLATFORM_A.
    assert stop_ids == {"STOP_PLATFORM_A", "STATION_A", "STOP_B", "STOP_C"}
    # Never-referenced and dropped-route-only stops must not survive.
    assert "STOP_UNUSED" not in stop_ids
    assert "STOP_D" not in stop_ids


def test_calendar_and_calendar_dates_filtered_independently(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    _, calendar = _read_output_rows(output_zip, "calendar.txt")
    assert {c["service_id"] for c in calendar} == {"S1", "S2"}

    _, calendar_dates = _read_output_rows(output_zip, "calendar_dates.txt")
    assert {c["service_id"] for c in calendar_dates} == {"S1"}
    # S3 (dropped route's service) and S4 (unrelated, calendar.txt-less
    # service) must both be gone.
    assert {c["service_id"] for c in calendar_dates}.isdisjoint({"S3", "S4"})


def test_shapes_referenced_by_kept_trips_survive(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    _, shapes = _read_output_rows(output_zip, "shapes.txt")
    assert {s["shape_id"] for s in shapes} == {"SH1"}


def test_agency_and_passthrough_file_survive_unfiltered(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    agency_fields, agency_rows = _read_output_rows(output_zip, "agency.txt")
    assert agency_fields == _FILES["agency.txt"][0]
    assert agency_rows == _FILES["agency.txt"][1]

    feed_info_fields, feed_info_rows = _read_output_rows(output_zip, "feed_info.txt")
    assert feed_info_fields == _FILES["feed_info.txt"][0]
    assert feed_info_rows == _FILES["feed_info.txt"][1]


def test_output_is_valid_zip_with_matching_headers(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["R1"])

    with zipfile.ZipFile(output_zip, "r") as zf:
        # No CRC/structure errors.
        assert zf.testzip() is None
        names = set(zf.namelist())

    assert names == set(_FILES.keys())
    for filename, (expected_fields, _) in _FILES.items():
        fieldnames, _ = _read_output_rows(output_zip, filename)
        assert fieldnames == expected_fields


def test_nonexistent_route_id_produces_empty_result(source_zip, tmp_path):
    output_zip = tmp_path / "out.zip"
    filter_gtfs_by_route(source_zip, output_zip, ["DOES_NOT_EXIST"])

    for filename in ("routes.txt", "trips.txt", "stop_times.txt", "stops.txt", "calendar.txt", "shapes.txt"):
        _, rows = _read_output_rows(output_zip, filename)
        assert rows == []

    # calendar_dates has no service_id tied to any kept trip either.
    _, calendar_dates = _read_output_rows(output_zip, "calendar_dates.txt")
    assert calendar_dates == []

    # Passthrough files are unaffected by an empty route match.
    _, agency_rows = _read_output_rows(output_zip, "agency.txt")
    assert agency_rows == _FILES["agency.txt"][1]
