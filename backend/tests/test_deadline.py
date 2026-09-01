import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import risk_engine
from app.deadline import compute_deadline_threshold
from app.models.transit import Itinerary, Leg
from app.route_index import RouteIndex
from db import get_connection

LOCAL_TZ = ZoneInfo("America/New_York")


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "deadline.sqlite3"))
    yield connection
    connection.close()


def _routes_zip(tmp_path, name: str, rows: list[tuple[str, str]]):
    """Same synthetic-GTFS-zip pattern as test_risk_engine.py's
    `_routes_zip` fixture -- avoids depending on the real ~150MB static
    GTFS files."""
    path = tmp_path / name
    lines = ["route_id,route_short_name,route_long_name,route_type"]
    for route_id, short_name in rows:
        lines.append(f"{route_id},{short_name},,1")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", "\n".join(lines))
    return path


@pytest.fixture
def route_index(tmp_path):
    subway_zip = _routes_zip(tmp_path, "subway.zip", [("F", "F"), ("Q", "Q")])
    bus_zip = _routes_zip(tmp_path, "bus.zip", [("Q102", "Q102")])
    return RouteIndex.from_gtfs([subway_zip, bus_zip])


def _local_ms(y, mo, d, h, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).timestamp() * 1000)


def _insert_bucket(conn, **overrides):
    fields = dict(
        agency="subway",
        route_id="F",
        stop_id="127N",
        direction="0",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": [0.0] * 101}),
        n_observations=250,
        n_ambiguous=0,
        window_start="2026-07-01",
        last_updated=datetime(2026, 8, 1).isoformat(),
    )
    fields.update(overrides)
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous, window_start,
            last_updated
        ) VALUES (
            :agency, :route_id, :stop_id, :direction, :day_type, :hour_bucket,
            :stat_type, :histogram, :n_observations, :n_ambiguous, :window_start,
            :last_updated
        )
        """,
        fields,
    )
    conn.commit()


# Worked example from task-4-brief.md: bin_width_s=30, min_s=-600, 250 total
# observations concentrated as counts[20]=200, counts[21]=50.
# threshold = 0.85 * 250 = 212.5
# cumulative after bin 20: 200 < 212.5 (not yet)
# cumulative after bin 21: 250 >= 212.5 -> return bin 21's upper edge
#   = -600 + 30*(21+1) = -600 + 660 = 60.0
_WORKED_EXAMPLE_COUNTS = [200.0 if i == 20 else (50.0 if i == 21 else 0.0) for i in range(101)]
_WORKED_EXAMPLE_P85_SECONDS = 60.0


def _subway_leg(route_short_name, from_id, to_id, from_name, to_name, start, end):
    return Leg(
        mode="SUBWAY",
        route_short_name=route_short_name,
        from_stop_id=f"MTA_NYCT_Subway:{from_id}",
        from_stop_name=from_name,
        to_stop_id=f"MTA_NYCT_Subway:{to_id}",
        to_stop_name=to_name,
        start_time_ms=start,
        end_time_ms=end,
    )


def _bus_leg(route_short_name, from_id, to_id, from_name, to_name, start, end):
    return Leg(
        mode="BUS",
        route_short_name=route_short_name,
        from_stop_id=f"MTABC:{from_id}",
        from_stop_name=from_name,
        to_stop_id=f"MTABC:{to_id}",
        to_stop_name=to_name,
        start_time_ms=start,
        end_time_ms=end,
    )


def _walk_leg(from_name, to_name, start, end):
    return Leg(
        mode="WALK",
        from_stop_name=from_name,
        to_stop_name=to_name,
        start_time_ms=start,
        end_time_ms=end,
    )


# --- single-leg, subway (delay stat_type), matches worked example ---------


def test_single_leg_subway_matches_worked_example(conn, route_index):
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    end = _local_ms(2026, 8, 24, 8, 10, 0)
    itinerary = Itinerary(
        duration_seconds=600,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), end),
        ],
    )

    deadline_ts = end + 3600_000  # deadline is a further hour past scheduled arrival
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)

    # p85_travel_time_ms = duration_seconds*1000 + total_p85_buffer_seconds*1000
    #                     = 600*1000 + 60.0*1000 = 660000
    # threshold_ts = deadline_ts - 660000
    expected = deadline_ts - round(600 * 1000 + _WORKED_EXAMPLE_P85_SECONDS * 1000)
    assert result == expected


# --- single-leg, bus (prediction_error stat_type) --------------------------


def test_single_leg_bus_uses_prediction_error_stat_type(conn, route_index):
    # Same histogram shape as the worked example, but stored as a bus
    # "prediction_error" bucket -- confirms _incoming_stat_type is genuinely
    # wired for agency selection, not hardcoded to "delay".
    _insert_bucket(
        conn,
        agency="bus",
        route_id="Q102",
        stop_id="450154",
        day_type="weekday",
        hour_bucket=8,
        stat_type="prediction_error",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    end = _local_ms(2026, 8, 24, 8, 10, 0)
    itinerary = Itinerary(
        duration_seconds=600,
        legs=[
            _bus_leg("Q102", "400000", "450154", "Start", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), end),
        ],
    )

    deadline_ts = end + 3600_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)

    expected = deadline_ts - round(600 * 1000 + _WORKED_EXAMPLE_P85_SECONDS * 1000)
    assert result == expected


# --- multi-leg: buffers summed across legs, not just the last leg used ----


def test_multi_leg_itinerary_sums_each_legs_p85_buffer(conn, route_index):
    # Leg 1 (subway, F): same worked-example histogram -> p85 buffer = 60.0s
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )
    # Leg 2 (subway, Q): distinct histogram, all 300 observations in bin 15
    # (bin 15 covers [-600+15*30, -600+16*30) = [-150, -120)).
    # threshold = 0.85*300 = 255; cumulative after bin 15 = 300 >= 255
    # -> upper edge = -600 + 30*16 = -600 + 480 = -120.0
    leg2_counts = [300.0 if i == 15 else 0.0 for i in range(101)]
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="201N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": leg2_counts}),
        n_observations=300,
    )

    leg1_end = _local_ms(2026, 8, 24, 8, 10, 0)
    leg2_start = _local_ms(2026, 8, 24, 8, 15, 0)
    leg2_end = _local_ms(2026, 8, 24, 8, 25, 0)

    itinerary = Itinerary(
        duration_seconds=1500,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), leg1_end),
            _walk_leg("Lexington Av/63 St", "Lexington Av/63 St", leg1_end, leg2_start),
            _subway_leg("Q", "127S", "201N", "Lexington Av/63 St", "Union Sq", leg2_start, leg2_end),
        ],
    )

    deadline_ts = leg2_end + 7200_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)

    total_p85_buffer_seconds = 60.0 + (-120.0)
    expected = deadline_ts - round(1500 * 1000 + total_p85_buffer_seconds * 1000)
    assert result == expected


# --- no matching bucket at all -> None for the whole itinerary ------------


def test_leg_with_no_matching_bucket_returns_none(conn, route_index):
    # Only leg 2 has a bucket; leg 1 has none at all.
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="201N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    leg1_end = _local_ms(2026, 8, 24, 8, 10, 0)
    leg2_start = _local_ms(2026, 8, 24, 8, 15, 0)
    leg2_end = _local_ms(2026, 8, 24, 8, 25, 0)

    itinerary = Itinerary(
        duration_seconds=1500,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), leg1_end),
            _subway_leg("Q", "127S", "201N", "Lexington Av/63 St", "Union Sq", leg2_start, leg2_end),
        ],
    )

    deadline_ts = leg2_end + 7200_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)
    assert result is None


# --- bucket present but n_observations < MIN_N_OBSERVATIONS -> None -------


def test_leg_with_insufficient_n_observations_returns_none(conn, route_index):
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": [50.0 if i == 20 else 0.0 for i in range(101)]}),
        n_observations=50,  # below risk_engine.MIN_N_OBSERVATIONS (200)
    )

    end = _local_ms(2026, 8, 24, 8, 10, 0)
    itinerary = Itinerary(
        duration_seconds=600,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), end),
        ],
    )

    deadline_ts = end + 3600_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)
    assert result is None
    assert 50 < risk_engine.MIN_N_OBSERVATIONS


# --- percentile boundary: cumulative == 0.85*total exactly -----------------


def test_percentile_boundary_exact_match_uses_that_bin():
    from app.deadline import _p85_seconds

    # 200 total observations: bin 5 gets 170, bin 6 gets 30.
    # threshold = 0.85 * 200 = 170.0 exactly.
    # cumulative after bin 5 = 170 >= 170.0 -> matches AT bin 5, not bin 6.
    counts = [170.0 if i == 5 else (30.0 if i == 6 else 0.0) for i in range(10)]
    histogram = {"bin_width_s": 30, "min_s": -600, "counts": counts}
    result = _p85_seconds(histogram)
    # bin 5's upper edge = -600 + 30*(5+1) = -600 + 180 = -420.0
    assert result == -420.0


# --- WALK leg mixed into a multi-leg itinerary is skipped -------------------


def test_walk_leg_is_skipped_not_treated_as_missing_bucket(conn, route_index):
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    leg1_start = _local_ms(2026, 8, 24, 8, 0, 0)
    leg1_end = _local_ms(2026, 8, 24, 8, 10, 0)
    walk_end = _local_ms(2026, 8, 24, 8, 15, 0)

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", leg1_start, leg1_end),
            _walk_leg("Lexington Av/63 St", "Destination", leg1_end, walk_end),
        ],
    )

    deadline_ts = walk_end + 3600_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index)

    # Only the subway leg contributes -- WALK leg has no bucket lookup at all,
    # so it must not cause a spurious None.
    expected = deadline_ts - round(900 * 1000 + _WORKED_EXAMPLE_P85_SECONDS * 1000)
    assert result == expected
