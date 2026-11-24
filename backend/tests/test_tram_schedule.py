import zipfile

import pytest

from app import tram_schedule


@pytest.fixture(autouse=True)
def _reset_cache():
    tram_schedule.reset_cache_for_tests()


def _write_tram_gtfs_zip(path, stop_id: str, weekday_times: list[str], weekend_times: list[str] | None = None):
    """A minimal synthetic tram GTFS zip with exactly the files
    _scheduled_headway_by_hour reads -- one stop, a weekday service and
    (optionally) a weekend one, one trip per listed departure time."""
    weekend_times = weekend_times or []
    with zipfile.ZipFile(path, "w") as z:
        calendar_lines = ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date"]
        calendar_lines.append("WKD,1,1,1,1,1,0,0,20260101,20261231")
        calendar_lines.append("WKND,0,0,0,0,0,1,1,20260101,20261231")
        trip_lines = ["trip_id,route_id,service_id,direction_id"]
        stop_time_lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
        for i, dep_time in enumerate(weekday_times):
            trip_id = f"WKD{i}"
            trip_lines.append(f"{trip_id},R,WKD,0")
            stop_time_lines.append(f"{trip_id},{dep_time},{dep_time},{stop_id},1")
        for i, dep_time in enumerate(weekend_times):
            trip_id = f"WKND{i}"
            trip_lines.append(f"{trip_id},R,WKND,0")
            stop_time_lines.append(f"{trip_id},{dep_time},{dep_time},{stop_id},1")
        z.writestr("calendar.txt", "\n".join(calendar_lines))
        z.writestr("trips.txt", "\n".join(trip_lines))
        z.writestr("stop_times.txt", "\n".join(stop_time_lines))


def test_typical_wait_seconds_is_real_gap_between_scheduled_departures(tmp_path, monkeypatch):
    # Every 7 minutes (420s) from 8:00 to 8:21 -- a real, exact headway,
    # not a guessed "tram headway" figure.
    zip_path = tmp_path / "tram.zip"
    _write_tram_gtfs_zip(zip_path, "STOP1", ["08:00:00", "08:07:00", "08:14:00", "08:21:00"])

    monkeypatch.setattr(tram_schedule, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(tram_schedule, "TRAM_ZIP_NAMES", ["tram.zip"])

    assert tram_schedule.typical_wait_seconds("STOP1", "weekday", 8) == 420.0


def test_typical_wait_seconds_is_none_for_unknown_stop(tmp_path, monkeypatch):
    zip_path = tmp_path / "tram.zip"
    _write_tram_gtfs_zip(zip_path, "STOP1", ["08:00:00", "08:07:00"])

    monkeypatch.setattr(tram_schedule, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(tram_schedule, "TRAM_ZIP_NAMES", ["tram.zip"])

    assert tram_schedule.typical_wait_seconds("NOT_A_REAL_STOP", "weekday", 8) is None


def test_typical_wait_seconds_is_none_for_hour_with_fewer_than_two_departures(tmp_path, monkeypatch):
    zip_path = tmp_path / "tram.zip"
    # Only one departure in hour 8 (the 09:00 one starts hour 9's own gap).
    _write_tram_gtfs_zip(zip_path, "STOP1", ["08:50:00", "09:00:00"])

    monkeypatch.setattr(tram_schedule, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(tram_schedule, "TRAM_ZIP_NAMES", ["tram.zip"])

    assert tram_schedule.typical_wait_seconds("STOP1", "weekday", 9) is None


def test_weekday_and_weekend_services_are_kept_separate(tmp_path, monkeypatch):
    zip_path = tmp_path / "tram.zip"
    _write_tram_gtfs_zip(
        zip_path, "STOP1",
        weekday_times=["08:00:00", "08:10:00"],  # 10-min weekday headway
        weekend_times=["08:00:00", "08:20:00"],  # 20-min weekend headway
    )

    monkeypatch.setattr(tram_schedule, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(tram_schedule, "TRAM_ZIP_NAMES", ["tram.zip"])

    assert tram_schedule.typical_wait_seconds("STOP1", "weekday", 8) == 600.0
    assert tram_schedule.typical_wait_seconds("STOP1", "weekend", 8) == 1200.0


def test_second_zip_in_scan_list_is_checked_when_stop_is_not_in_the_first(tmp_path, monkeypatch):
    zip_a = tmp_path / "a.zip"
    zip_b = tmp_path / "b.zip"
    _write_tram_gtfs_zip(zip_a, "STOP_A", ["08:00:00", "08:10:00"])
    _write_tram_gtfs_zip(zip_b, "STOP_B", ["08:00:00", "08:15:00"])

    monkeypatch.setattr(tram_schedule, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(tram_schedule, "TRAM_ZIP_NAMES", ["a.zip", "b.zip"])

    assert tram_schedule.typical_wait_seconds("STOP_B", "weekday", 8) == 900.0
