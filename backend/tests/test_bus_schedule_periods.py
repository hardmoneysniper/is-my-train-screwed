import zipfile

import pytest

from app import bus_schedule_periods


@pytest.fixture(autouse=True)
def _reset_cache():
    bus_schedule_periods.reset_cache_for_tests()


# --- _cluster_into_periods: pure unit tests, no GTFS involved -------------


def test_cluster_into_periods_merges_similar_adjacent_hours():
    # A flat 20-min headway from hour 5 through hour 10 -- all within
    # PERIOD_MERGE_TOLERANCE of the previous hour (0% change) -- must all
    # collapse into one period, keyed by the first hour walked (5).
    headway = {5: 20.0, 6: 20.0, 7: 20.0, 8: 20.0, 9: 20.0, 10: 20.0}
    periods = bus_schedule_periods._cluster_into_periods(headway)
    assert periods == {5: 5, 6: 5, 7: 5, 8: 5, 9: 5, 10: 5}


def test_cluster_into_periods_splits_on_big_jump():
    # hour 8 -> 9 is a 100% jump (10 -> 20 min), well past the 30%
    # tolerance -- must start a new period at hour 9.
    headway = {5: 10.0, 6: 10.0, 7: 10.0, 8: 10.0, 9: 20.0, 10: 20.0}
    periods = bus_schedule_periods._cluster_into_periods(headway)
    assert periods == {5: 5, 6: 5, 7: 5, 8: 5, 9: 9, 10: 9}


def test_cluster_into_periods_wraps_through_midnight_starting_at_anchor():
    # Walk order starts at ANCHOR_HOUR=5 and wraps 23 -> 0 -> ... -> 4.
    # Only hours 22, 23, 0 have data (a real "night" stretch spanning
    # midnight) -- must merge into one period despite the 23/0 wraparound.
    headway = {22: 30.0, 23: 30.0, 0: 30.0}
    periods = bus_schedule_periods._cluster_into_periods(headway)
    assert periods == {22: 22, 23: 22, 0: 22}


def test_cluster_into_periods_empty_input_returns_empty():
    assert bus_schedule_periods._cluster_into_periods({}) == {}


def test_cluster_into_periods_single_hour():
    assert bus_schedule_periods._cluster_into_periods({8: 15.0}) == {8: 8}


# --- period_hour_for: integration with real (synthetic) GTFS parsing -----


def _write_bus_gtfs_zip(path, route_id: str, weekday_departure_times: list[str]):
    """A minimal synthetic bus GTFS zip with exactly the files
    _scheduled_headway_by_hour reads -- one route, one weekday service,
    one trip per listed departure time, all at the same representative
    stop (so stop_counts naturally picks it as the busiest)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", f"route_id,route_short_name,route_long_name,route_type\n{route_id},{route_id},,3\n")
        z.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WKD,1,1,1,1,1,0,0,20260101,20261231\n",
        )
        trip_lines = ["trip_id,route_id,service_id,direction_id"]
        stop_time_lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
        for i, dep_time in enumerate(weekday_departure_times):
            trip_id = f"T{i}"
            trip_lines.append(f"{trip_id},{route_id},WKD,0")
            stop_time_lines.append(f"{trip_id},{dep_time},{dep_time},REP_STOP,1")
        z.writestr("trips.txt", "\n".join(trip_lines))
        z.writestr("stop_times.txt", "\n".join(stop_time_lines))


def test_period_hour_for_derives_real_periods_from_synthetic_gtfs(tmp_path, monkeypatch):
    # Departures every 10 min from 7:00-9:00 (10-min headway, hours 7-8),
    # then every 30 min from 12:00-13:30 (hour 12) -- a real, sharp
    # schedule change a route-specific analysis must pick up. The 09:00
    # departure exists only to close hour 8's last real interval cleanly
    # at 10 min -- without it, the big 09:00-12:00 gap between blocks
    # would leak into (and corrupt) hour 8's average instead of landing
    # on the unsampled hour 9.
    rush_times = ["07:00:00", "07:10:00", "07:20:00", "07:30:00", "07:40:00", "07:50:00",
                  "08:00:00", "08:10:00", "08:20:00", "08:30:00", "08:40:00", "08:50:00", "09:00:00"]
    midday_times = ["12:00:00", "12:30:00", "13:00:00", "13:30:00"]
    zip_path = tmp_path / "bus.zip"
    _write_bus_gtfs_zip(zip_path, "T1", sorted(rush_times + midday_times))

    monkeypatch.setattr(bus_schedule_periods, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(bus_schedule_periods, "BUS_ZIP_NAMES", ["bus.zip"])

    # Rush hours (7, 8) should share a period; midday (12) should be a
    # different one, given the real scheduled headway actually changes
    # from 10 min to 30 min between them.
    assert bus_schedule_periods.period_hour_for("T1", "weekday", 7) == bus_schedule_periods.period_hour_for("T1", "weekday", 8)
    assert bus_schedule_periods.period_hour_for("T1", "weekday", 12) != bus_schedule_periods.period_hour_for("T1", "weekday", 7)


def test_period_hour_for_falls_back_to_literal_hour_when_route_unknown(tmp_path, monkeypatch):
    zip_path = tmp_path / "bus.zip"
    _write_bus_gtfs_zip(zip_path, "REAL_ROUTE", ["08:00:00", "08:10:00"])
    monkeypatch.setattr(bus_schedule_periods, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(bus_schedule_periods, "BUS_ZIP_NAMES", ["bus.zip"])

    # "UNKNOWN_ROUTE" isn't in the synthetic GTFS at all -- no schedule
    # evidence exists, must degrade to the literal hour, never guess.
    assert bus_schedule_periods.period_hour_for("UNKNOWN_ROUTE", "weekday", 14) == 14


def test_period_hour_for_falls_back_when_hour_has_no_scheduled_service(tmp_path, monkeypatch):
    zip_path = tmp_path / "bus.zip"
    _write_bus_gtfs_zip(zip_path, "T1", ["08:00:00", "08:10:00"])
    monkeypatch.setattr(bus_schedule_periods, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(bus_schedule_periods, "BUS_ZIP_NAMES", ["bus.zip"])

    # Hour 3 has zero real scheduled departures for this route -- no
    # evidence to justify merging it with anything, must stay literal.
    assert bus_schedule_periods.period_hour_for("T1", "weekday", 3) == 3


def _write_routes_only_zip(path, route_id: str):
    """A zip whose routes.txt lists route_id but has no trips.txt/
    stop_times.txt rows for it at all -- the real CLAUDE.md-documented
    gotcha (e.g. M60+ appears in every borough's routes.txt but has real
    trips in only one)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", f"route_id,route_short_name,route_long_name,route_type\n{route_id},{route_id},,3\n")
        z.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WKD,1,1,1,1,1,0,0,20260101,20261231\n",
        )
        z.writestr("trips.txt", "trip_id,route_id,service_id,direction_id\n")
        z.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n")


def test_scheduled_headway_skips_a_zip_where_the_route_has_zero_real_trips(tmp_path, monkeypatch):
    """Regression test for a real bug: routes.txt listing route_id in a
    zip does NOT mean that zip has real trips for it -- the first version
    of this function committed to the first zip where routes.txt merely
    mentioned the route, then gave up entirely if trips.txt/stop_times.txt
    turned out empty, instead of trying the next zip. Confirmed live: this
    silently zeroed out periods for every real multi-zip-listed route
    (Q90, M60+) before the fix."""
    _write_routes_only_zip(tmp_path / "bus_empty.zip", "T1")
    _write_bus_gtfs_zip(tmp_path / "bus_real.zip", "T1", ["08:00:00", "08:10:00"])
    monkeypatch.setattr(bus_schedule_periods, "GTFS_DIR", tmp_path)
    # Empty-for-this-route zip listed FIRST -- must not cause the search
    # to give up before reaching bus_real.zip.
    monkeypatch.setattr(bus_schedule_periods, "BUS_ZIP_NAMES", ["bus_empty.zip", "bus_real.zip"])

    headway = bus_schedule_periods._scheduled_headway_by_hour("T1", "weekday")
    assert headway == {8: 10.0}


def test_period_hour_for_caches_per_route_and_day_type(tmp_path, monkeypatch):
    zip_path = tmp_path / "bus.zip"
    _write_bus_gtfs_zip(zip_path, "T1", ["08:00:00", "08:10:00"])
    monkeypatch.setattr(bus_schedule_periods, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(bus_schedule_periods, "BUS_ZIP_NAMES", ["bus.zip"])

    bus_schedule_periods.period_hour_for("T1", "weekday", 8)
    assert ("T1", "weekday") in bus_schedule_periods._period_cache
    # A second call for the same (route, day_type) must not need to
    # re-parse the GTFS -- delete the zip and confirm it still works from cache.
    zip_path.unlink()
    assert bus_schedule_periods.period_hour_for("T1", "weekday", 8) == 8
