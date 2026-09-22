# backend/tests/test_main.py
"""Task 10: routing-wiring for the mounted realtime proxy, and error
handling for the in-process bus-collector loop. Not a re-test of the
proxy's own trip-id-matching logic (test_realtime_proxy.py) or the bus
collector's own logic (test_bus_collector.py). The nightly subway
backfill/ingest + reliability_buckets aggregation that used to run here
(and had tests here) moved to a local script -- see
scripts/run_local_pipeline.py and its own test file -- after the
2026-09-22 cost incident documented in main.py and CLAUDE.md."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import _run_bus_collector_loop, app


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
