# backend/tests/test_replan_agent.py
import json
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app import risk_engine
from app.agents.conversation_agent import CITATION_FOOTER_TEMPLATE
from app.agents.replan_agent import replan_trip
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary, Leg
from app.monitoring import create_monitored_trip
from app.route_index import RouteIndex
from app.routing.nearest_stop import StopIndex
from db import get_connection

LOCAL_TZ = ZoneInfo("America/New_York")


# --- fixtures ----------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "replan.sqlite3"))
    yield connection
    connection.close()


@pytest.fixture
def route_index():
    # Synthetic RouteIndex built directly (no zip needed -- the constructor
    # takes the same list-of-dicts shape from_gtfs produces internally,
    # matching test_deadline.py's/test_risk_engine.py's synthetic-index
    # convention, just skipping the zip-writing step since RouteIndex's
    # constructor itself needs no zip parsing to exercise).
    return RouteIndex([
        {"route_id": "F", "route_short_name": "F"},
        {"route_id": "Q", "route_short_name": "Q"},
    ])


@pytest.fixture
def stop_index():
    return StopIndex([
        {"stop_id": "A1", "stop_name": "Roosevelt Island", "lat": 40.7597, "lon": -73.9532},
        {"stop_id": "A2", "stop_name": "Transfer Stop", "lat": 40.7600, "lon": -73.9600},
        {"stop_id": "A3", "stop_name": "Lex/63", "lat": 40.7644, "lon": -73.9656},
    ])


@pytest.fixture(autouse=True)
def patch_indexes(monkeypatch, route_index, stop_index):
    # get_risk/compute_deadline_threshold both default to
    # risk_engine._default_route_index() when no route_index is passed
    # in -- replan_trip never passes one explicitly (matching every other
    # consumer's convention), so patch the shared lazy-cache function
    # itself rather than teaching replan_trip a route_index= parameter
    # the brief doesn't specify. This single patch covers both call sites
    # (deadline.py calls risk_engine._default_route_index() directly).
    monkeypatch.setattr(risk_engine, "_default_route_index", lambda: route_index)
    monkeypatch.setattr("app.agents.replan_agent.get_stop_index", lambda: stop_index)


# --- helpers -------------------------------------------------------------


def _local_ms(y, mo, d, h, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).timestamp() * 1000)


def _subway_leg(route_short_name, from_id, to_id, from_name, to_name, start, end):
    return Leg(
        mode="SUBWAY",
        route_short_name=route_short_name,
        from_stop_id=f"mtasbwy:{from_id}",
        from_stop_name=from_name,
        to_stop_id=f"mtasbwy:{to_id}",
        to_stop_name=to_name,
        start_time_ms=start,
        end_time_ms=end,
    )


def _walk_leg(from_name, to_name, start, end):
    return Leg(mode="WALK", from_stop_name=from_name, to_stop_name=to_name, start_time_ms=start, end_time_ms=end)


def _insert_bucket(conn, **overrides):
    fields = dict(
        agency="subway",
        route_id="F",
        stop_id="A2",
        direction="0",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": [0.0] * 101}),
        n_observations=250,
        n_ambiguous=0,
        window_start="2026-07-01",  # far enough in the past to hit the 20-day cap regardless of "today"
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


def _create_trip(conn, itinerary, deadline_ts=None, anonymous_id="anon-1") -> MonitoredTrip:
    trip_id = create_monitored_trip(itinerary, anonymous_id, deadline_ts, conn=conn)
    return _fetch_trip(conn, trip_id)


def _fetch_trip(conn, trip_id) -> MonitoredTrip:
    row = conn.execute("SELECT * FROM monitored_trips WHERE id = ?", (trip_id,)).fetchone()
    row_dict = dict(row)
    row_dict["itinerary_snapshot"] = Itinerary.model_validate_json(row_dict["itinerary_snapshot"])
    return MonitoredTrip.model_validate(row_dict)


def _expected_clock(depart_by_ts: int) -> str:
    # Independent (non-imported) reimplementation of the clock-formatting
    # rule, so this test genuinely checks replan_agent's output rather than
    # just re-invoking the same private helper it's testing.
    dt = datetime.fromtimestamp(depart_by_ts / 1000, tz=timezone.utc).astimezone(LOCAL_TZ)
    hour_minute = dt.strftime("%I:%M %p")
    return hour_minute[1:] if hour_minute.startswith("0") else hour_minute


_WORKED_EXAMPLE_COUNTS = [200.0 if i == 20 else (50.0 if i == 21 else 0.0) for i in range(101)]  # p85 = 60.0s
_OTHER_COUNTS = [300.0 if i == 15 else 0.0 for i in range(101)]  # p85 = -120.0s


# --- Test 1: zero-transfer trip reroutes into a real transfer risk -------


@pytest.mark.asyncio
async def test_zero_transfer_reroutes_into_transfer_with_citation(conn, route_index):
    old_itinerary = Itinerary(
        duration_seconds=1200,
        legs=[_subway_leg("F", "A1", "A3", "Roosevelt Island", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 20))],
    )
    trip = _create_trip(conn, old_itinerary)

    new_itinerary = Itinerary(
        duration_seconds=1200,
        legs=[
            _subway_leg("F", "A1", "A2", "Roosevelt Island", "Transfer Stop",
                        _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 8)),
            _subway_leg("Q", "A2", "A3", "Transfer Stop", "Lex/63",
                        _local_ms(2026, 8, 24, 8, 10), _local_ms(2026, 8, 24, 8, 20)),
        ],
    )
    # Incoming (F, delay -- subway) and outgoing (Q, headway) buckets at the
    # new route's transfer stop -- both sufficient (nonzero histogram mass),
    # so get_risk returns quality="ok".
    _insert_bucket(
        conn, agency="subway", route_id="F", stop_id="A2", stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )
    _insert_bucket(
        conn, agency="subway", route_id="Q", stop_id="A2", stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [300.0 if i == 4 else 0.0 for i in range(81)]}),
        n_observations=300,
    )

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [new_itinerary]
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is not None
    assert re.search(r"\d+%\*", result)

    # n/window_days for the footer come straight from a deterministic bucket
    # lookup (only p_miss itself is randomized by the Monte Carlo), so a
    # fresh get_risk call is a safe, non-flaky source of the expected footer.
    expected_risks = risk_engine.get_risk(new_itinerary, conn=conn, route_index=route_index)
    ok_risk = next(r for r in expected_risks if r.quality == "ok")
    expected_footer = CITATION_FOOTER_TEMPLATE.format(n=round(ok_risk.n), window_days=ok_risk.window_days)
    assert expected_footer in result

    row = conn.execute("SELECT itinerary_snapshot FROM monitored_trips WHERE id = ?", (trip.id,)).fetchone()
    assert Itinerary.model_validate_json(row["itinerary_snapshot"]) == new_itinerary


# --- Test 2: transfer trip reroutes into a zero-transfer trip ------------


@pytest.mark.asyncio
async def test_transfer_reroutes_into_zero_transfer_no_citation(conn):
    old_itinerary = Itinerary(
        duration_seconds=1200,
        legs=[
            _subway_leg("F", "A1", "A2", "Roosevelt Island", "Transfer Stop",
                        _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 8)),
            _subway_leg("Q", "A2", "A3", "Transfer Stop", "Lex/63",
                        _local_ms(2026, 8, 24, 8, 10), _local_ms(2026, 8, 24, 8, 20)),
        ],
    )
    trip = _create_trip(conn, old_itinerary)

    new_itinerary = Itinerary(
        duration_seconds=1200,
        legs=[_subway_leg("F", "A1", "A3", "Roosevelt Island", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 20))],
    )

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [new_itinerary]
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is not None
    assert "%*" not in result
    assert "Based on" not in result
    assert "No transfer risk to report for the new route." in result

    row = conn.execute("SELECT itinerary_snapshot FROM monitored_trips WHERE id = ?", (trip.id,)).fetchone()
    assert Itinerary.model_validate_json(row["itinerary_snapshot"]) == new_itinerary


# --- Test 3: deadline-mode trip reroutes, recomputed against the NEW route


@pytest.mark.asyncio
async def test_deadline_mode_recomputes_against_new_itinerary(conn, route_index):
    old_itinerary = Itinerary(
        duration_seconds=600,
        legs=[_subway_leg("F", "A1", "A3", "Roosevelt Island", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 10))],
    )
    deadline_ts_ms = _local_ms(2026, 8, 24, 10, 0)
    trip = _create_trip(conn, old_itinerary, deadline_ts=deadline_ts_ms)

    new_itinerary = Itinerary(
        duration_seconds=900,
        legs=[_subway_leg("Q", "A1", "A2", "Roosevelt Island", "Transfer Stop",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 15))],
    )

    # Old route's arrival stop (A3, route F): p85 = -120.0s (worked example
    # from task-4-brief.md's sibling histogram shape).
    _insert_bucket(
        conn, agency="subway", route_id="F", stop_id="A3", stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _OTHER_COUNTS}),
        n_observations=300,
    )
    # New route's arrival stop (A2, route Q): p85 = 60.0s.
    _insert_bucket(
        conn, agency="subway", route_id="Q", stop_id="A2", stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    from app import deadline as deadline_module

    expected_new_depart_by = deadline_module.compute_deadline_threshold(
        new_itinerary, deadline_ts_ms, conn=conn, route_index=route_index
    )
    expected_old_depart_by = deadline_module.compute_deadline_threshold(
        old_itinerary, deadline_ts_ms, conn=conn, route_index=route_index
    )
    assert expected_new_depart_by != expected_old_depart_by

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [new_itinerary]
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is not None
    assert _expected_clock(expected_new_depart_by) in result
    assert _expected_clock(expected_old_depart_by) not in result


# --- Test 4: no real change -- snapshot updates, no notification ---------


@pytest.mark.asyncio
async def test_no_real_change_updates_snapshot_but_no_notification(conn):
    old_itinerary = Itinerary(
        duration_seconds=600,
        legs=[_subway_leg("F", "A1", "A3", "Roosevelt Island", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 10))],
    )
    trip = _create_trip(conn, old_itinerary)
    conn.execute("UPDATE monitored_trips SET pending_notification = ? WHERE id = ?", ("SENTINEL", trip.id))
    conn.commit()

    # Same route signature (SUBWAY, "F"), only timing refreshed.
    new_itinerary = Itinerary(
        duration_seconds=540,
        legs=[_subway_leg("F", "A1", "A3", "Roosevelt Island", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 2), _local_ms(2026, 8, 24, 8, 11))],
    )

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [new_itinerary]
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is None

    row = conn.execute("SELECT itinerary_snapshot, pending_notification FROM monitored_trips WHERE id = ?", (trip.id,)).fetchone()
    assert Itinerary.model_validate_json(row["itinerary_snapshot"]) == new_itinerary
    assert row["pending_notification"] == "SENTINEL"


# --- Test 5: unresolvable stop -- no plan_route call, no DB write --------


@pytest.mark.asyncio
async def test_unresolvable_stop_skips_without_calling_plan_route_or_writing(conn):
    old_itinerary = Itinerary(
        duration_seconds=600,
        legs=[_subway_leg("F", "GHOST", "A3", "Nowhere", "Lex/63",
                           _local_ms(2026, 8, 24, 8, 0), _local_ms(2026, 8, 24, 8, 10))],
    )
    trip = _create_trip(conn, old_itinerary)

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is None
    mock_plan.assert_not_called()

    row = conn.execute("SELECT itinerary_snapshot, pending_notification FROM monitored_trips WHERE id = ?", (trip.id,)).fetchone()
    assert Itinerary.model_validate_json(row["itinerary_snapshot"]) == old_itinerary
    assert row["pending_notification"] is None


# --- Test 6: all-WALK itinerary -- no transit legs at all ----------------


@pytest.mark.asyncio
async def test_all_walk_itinerary_skips_without_calling_plan_route(conn):
    old_itinerary = Itinerary(
        duration_seconds=300,
        legs=[_walk_leg("A", "B", 0, 300_000)],
    )
    trip = _create_trip(conn, old_itinerary)

    with patch("app.agents.replan_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        result = await replan_trip(trip, "alert", conn=conn)

    assert result is None
    mock_plan.assert_not_called()
