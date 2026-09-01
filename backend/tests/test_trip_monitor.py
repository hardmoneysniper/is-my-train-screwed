# backend/tests/test_trip_monitor.py
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.alerts import AlertRecord
from app.models.transit import Itinerary, Leg
from app.monitoring import create_monitored_trip
from app.route_index import RouteIndex
from app.trip_monitor import run_monitor_cycle
from db import get_connection


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "trip_monitor.sqlite3"))
    yield connection
    connection.close()


@pytest.fixture
def route_index():
    # Synthetic RouteIndex, same convention as test_replan_agent.py -- no
    # zip parsing needed to exercise RouteIndex.resolve.
    return RouteIndex([
        {"route_id": "F", "route_short_name": "F"},
        {"route_id": "Q70-SBS", "route_short_name": "Q70+"},
    ])


def _future_ms(hours=1) -> int:
    return int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp() * 1000)


def _past_ms(hours=1) -> int:
    return int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)


def _subway_itinerary(end_ms) -> Itinerary:
    return Itinerary(
        duration_seconds=1200,
        legs=[Leg(
            mode="SUBWAY", route_short_name="F", from_stop_id="mtasbwy:A1", from_stop_name="Roosevelt Island",
            to_stop_id="mtasbwy:A3", to_stop_name="Lex/63", start_time_ms=end_ms - 1200_000, end_time_ms=end_ms,
        )],
    )


def _bus_itinerary(end_ms) -> Itinerary:
    return Itinerary(
        duration_seconds=1200,
        legs=[Leg(
            mode="BUS", route_short_name="Q70+", from_stop_id="MTA_1", from_stop_name="LGA",
            to_stop_id="MTA_2", to_stop_name="Jackson Heights", start_time_ms=end_ms - 1200_000, end_time_ms=end_ms,
        )],
    )


def _create_trip(conn, itinerary, anonymous_id="anon-1") -> int:
    # end_time_ms + 30min is create_monitored_trip's own ttl_expires_at
    # rule (monitoring.py) -- callers here choose end_ms far enough in the
    # future/past so the resulting ttl_expires_at lands on the intended
    # side of "now" without duplicating that +30min arithmetic per test.
    return create_monitored_trip(itinerary, anonymous_id, deadline_ts=None, conn=conn)


def _alert(alert_id, route_ids, header_text="Delays", active=True) -> AlertRecord:
    return AlertRecord(alert_id=alert_id, route_ids=route_ids, stop_ids=[], header_text=header_text, active=active)


# --- Test 1: matching alert triggers exactly one replan_trip call, even --
# --- with 2+ matching alert entities -------------------------------------


@pytest.mark.asyncio
async def test_matching_alert_calls_replan_trip_exactly_once(conn, route_index):
    trip_id = _create_trip(conn, _subway_itinerary(_future_ms(2)))

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.return_value = [
            _alert("a1", ["F"], header_text="Signal problems"),
            _alert("a2", ["F"], header_text="Delays"),
        ]
        mock_bus.return_value = []
        mock_replan.return_value = "Your trip has changed."

        summary = await run_monitor_cycle(conn, route_index=route_index)

    assert mock_replan.call_count == 1
    assert summary["trips_claimed"] == 1
    assert summary["trips_replanned"] == 1
    assert summary["trips_expired"] == 0
    assert summary["trips_failed"] == 0


# --- Test 2: expired trip is swept, never replanned, even with a match ---


@pytest.mark.asyncio
async def test_expired_trip_is_swept_and_never_replanned(conn, route_index):
    trip_id = _create_trip(conn, _subway_itinerary(_past_ms(1)))
    # _past_ms(1) -> ttl_expires_at = 1 hour ago + 30 min = 30 min ago:
    # already expired at cycle time.

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.return_value = [_alert("a1", ["F"])]
        mock_bus.return_value = []

        summary = await run_monitor_cycle(conn, route_index=route_index)

    mock_replan.assert_not_called()
    assert summary["trips_expired"] == 1
    assert summary["trips_replanned"] == 0

    row = conn.execute("SELECT status FROM monitored_trips WHERE id = ?", (trip_id,)).fetchone()
    assert row["status"] == "expired"


# --- Test 3: two trips on the same route share one alerts-feed fetch ----


@pytest.mark.asyncio
async def test_two_trips_same_route_share_one_alerts_fetch(conn, route_index):
    _create_trip(conn, _subway_itinerary(_future_ms(2)), anonymous_id="anon-1")
    _create_trip(conn, _subway_itinerary(_future_ms(2)), anonymous_id="anon-2")

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.return_value = [_alert("a1", ["F"])]
        mock_bus.return_value = []
        mock_replan.return_value = "changed"

        summary = await run_monitor_cycle(conn, route_index=route_index)

    mock_subway.assert_called_once()
    mock_bus.assert_called_once()
    assert mock_replan.call_count == 2
    assert summary["trips_claimed"] == 2


# --- Test 4: one trip's failure doesn't stop the other from processing --


@pytest.mark.asyncio
async def test_one_trip_failure_does_not_block_the_other(conn, route_index):
    failing_id = _create_trip(conn, _subway_itinerary(_future_ms(2)), anonymous_id="anon-fail")
    healthy_id = _create_trip(conn, _subway_itinerary(_future_ms(2)), anonymous_id="anon-ok")

    async def _replan_side_effect(trip, reason, conn=None):
        if trip.id == failing_id:
            raise RuntimeError("boom")
        return "changed"

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.return_value = [_alert("a1", ["F"])]
        mock_bus.return_value = []
        mock_replan.side_effect = _replan_side_effect

        summary = await run_monitor_cycle(conn, route_index=route_index)

    called_trip_ids = {call.args[0].id for call in mock_replan.call_args_list}
    assert failing_id in called_trip_ids
    assert healthy_id in called_trip_ids
    assert summary["trips_failed"] == 1
    assert summary["trips_replanned"] == 1
    assert summary["trips_claimed"] == 2


# --- Test 5: concurrency -- two real connections, one alert-matching trip,
# --- replan_trip called exactly once in total across both cycles --------


@pytest.mark.asyncio
async def test_concurrent_cycles_only_replan_once(tmp_path, route_index):
    db_path = str(tmp_path / "trip_monitor_concurrent.sqlite3")
    setup_conn = get_connection(db_path)
    trip_id = _create_trip(setup_conn, _subway_itinerary(_future_ms(2)))
    setup_conn.close()

    conn_a = get_connection(db_path)
    conn_b = get_connection(db_path)

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.return_value = [_alert("a1", ["F"])]
        mock_bus.return_value = []
        mock_replan.return_value = "changed"

        try:
            await asyncio.gather(
                run_monitor_cycle(conn_a, route_index=route_index),
                run_monitor_cycle(conn_b, route_index=route_index),
            )
        finally:
            conn_a.close()
            conn_b.close()

    assert mock_replan.call_count == 1


# --- Test 6: subway alert fetch failure doesn't block a bus trip's cycle -


@pytest.mark.asyncio
async def test_subway_fetch_failure_does_not_block_bus_trip(conn, route_index):
    trip_id = _create_trip(conn, _bus_itinerary(_future_ms(2)))

    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus, \
         patch("app.trip_monitor.replan_trip", new_callable=AsyncMock) as mock_replan:
        mock_subway.side_effect = RuntimeError("subway feed down")
        mock_bus.return_value = [_alert("b1", ["Q70-SBS"])]
        mock_replan.return_value = "changed"

        summary = await run_monitor_cycle(conn, route_index=route_index)

    mock_replan.assert_called_once()
    assert summary["trips_replanned"] == 1
    assert summary["trips_failed"] == 0


# --- Test 7: no claimed trips skips the alerts fetch entirely -----------


@pytest.mark.asyncio
async def test_no_claimed_trips_skips_alerts_fetch(conn, route_index):
    with patch("app.trip_monitor.fetch_subway_alerts", new_callable=AsyncMock) as mock_subway, \
         patch("app.trip_monitor.fetch_bus_alerts", new_callable=AsyncMock) as mock_bus:
        summary = await run_monitor_cycle(conn, route_index=route_index)

    mock_subway.assert_not_called()
    mock_bus.assert_not_called()
    assert summary == {
        "trips_claimed": 0,
        "trips_expired": 0,
        "trips_replanned": 0,
        "trips_failed": 0,
    }
