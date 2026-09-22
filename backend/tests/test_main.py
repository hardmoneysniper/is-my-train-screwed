# backend/tests/test_main.py
"""Task 10: routing-wiring for the mounted realtime proxy, and error
handling for the in-process nightly aggregation loop. Not a re-test of
the proxy's own trip-id-matching logic (test_realtime_proxy.py) or the
aggregation fold logic (test_aggregate_reliability_buckets.py)."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import _run_aggregation_loop, _run_aggregation_sync, _run_bus_collector_loop, app


def test_proxy_rt_route_reachable_through_mount():
    # Entering via `with` runs app.main's lifespan, which drives the
    # mounted realtime-proxy sub-app's own lifespan (loading its
    # TripIndex from the real subway.zip) -- if that wiring were broken,
    # this would raise on __enter__ rather than return a proxy-specific
    # 404 below.
    with TestClient(app) as client:
        response = client.get("/proxy/rt/unknown-feed")
    # The proxy's own handler rejects unknown feed groups before any
    # network call -- a 404 with this specific detail proves the request
    # reached realtime_proxy's route (not the outer app's generic 404).
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown feed group 'unknown-feed'"


async def test_aggregation_loop_survives_a_failed_run_and_keeps_going():
    fake_conn = MagicMock()
    call_count = {"n": 0}

    def failing_run_aggregate(conn):
        call_count["n"] += 1
        raise RuntimeError("boom")

    async def sleep_then_stop(seconds):
        # Let exactly one failed cycle complete, then break out of the
        # otherwise-infinite loop so the test doesn't hang.
        raise asyncio.CancelledError()

    with patch("app.main.get_connection", return_value=fake_conn), \
         patch("app.main.run_aggregate", side_effect=failing_run_aggregate), \
         patch("app.main.asyncio.sleep", side_effect=sleep_then_stop), \
         patch("app.main.logging.exception") as mock_log_exception:
        with pytest.raises(asyncio.CancelledError):
            await _run_aggregation_loop()

    # The failure was caught (not propagated out of the try block) and
    # logged, the connection was still closed, and the loop reached
    # asyncio.sleep -- i.e. it moved on to schedule the next cycle
    # instead of dying.
    assert call_count["n"] == 1
    mock_log_exception.assert_called_once()
    fake_conn.close.assert_called_once()


async def test_bus_collector_loop_restarts_immediately_after_a_failure():
    call_count = {"n": 0}

    def failing_run_forever():
        call_count["n"] += 1
        raise RuntimeError("boom")

    sleep_calls = []

    async def record_sleep_then_stop(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    with patch("app.main.run_forever", side_effect=failing_run_forever), \
         patch("app.main.asyncio.sleep", side_effect=record_sleep_then_stop), \
         patch("app.main.logging.exception") as mock_log_exception:
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_collector_loop()

    assert call_count["n"] == 1
    mock_log_exception.assert_called_once()
    # Restarts immediately on failure, not on the 24h aggregation cadence --
    # confirm whatever sleep duration is used is small (seconds, not a day).
    assert sleep_calls == [] or sleep_calls[0] < 3600


async def test_nightly_ingestion_steps_are_independently_isolated():
    fake_conn = MagicMock()
    calls = []

    def failing_backfill(*a, **k):
        calls.append("backfill")
        raise RuntimeError("subway backfill boom")

    def ok_ingest(*a, **k):
        calls.append("ingest")

    def failing_derive(*a, **k):
        calls.append("derive")
        raise RuntimeError("bus derive boom")

    def ok_aggregate(conn):
        calls.append("aggregate")

    with patch("app.main.get_connection", return_value=fake_conn), \
         patch("app.main.download_subwaydata_run_backfill", side_effect=failing_backfill), \
         patch("app.main.ingest_subwaydata_run_ingest", side_effect=ok_ingest), \
         patch("app.main.derive_bus_arrival_events_run_derive", side_effect=failing_derive), \
         patch("app.main.run_aggregate", side_effect=ok_aggregate), \
         patch("app.main.logging.exception") as mock_log_exception:
        _run_aggregation_sync()

    # All four steps were attempted despite two of them failing -- neither
    # failure blocked its siblings or the final aggregate call.
    assert calls == ["backfill", "ingest", "derive", "aggregate"]
    assert mock_log_exception.call_count == 2
    fake_conn.close.assert_called_once()
