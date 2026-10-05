# backend/tests/test_main.py
"""Task 10: routing-wiring for the mounted realtime proxy, and error
handling for the in-process bus-collector loop. Not a re-test of the
proxy's own trip-id-matching logic (test_realtime_proxy.py) or the bus
collector's own logic (test_bus_collector.py). The nightly subway
backfill/ingest + reliability_buckets aggregation that used to run here
(and had tests here) moved to a local script -- see
scripts/run_local_pipeline.py and its own test file -- after the
2026-09-22 cost incident documented in main.py and CLAUDE.md. The
per-corridor auto-stop target-check that used to run here (and had
tests here) moved to scripts/hourly_bus_sync.py -- see its own test
file -- after the 2026-09-28 fix documented in main.py: this service's
own view of collected data was being reset hourly by the local sync
daemon and could never reach a real cumulative target."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import _run_bus_collector_loop, _run_bus_volume_check_loop, app


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

    def failing_run_forever(should_continue=None):
        call_count["n"] += 1
        raise RuntimeError("boom")

    sleep_calls = []

    async def record_sleep_then_stop(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    with patch("app.main._is_weekend", return_value=True), \
         patch("app.main.run_forever", side_effect=failing_run_forever), \
         patch("app.main.asyncio.sleep", side_effect=record_sleep_then_stop), \
         patch("app.main.logging.exception") as mock_log_exception:
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_collector_loop()

    assert call_count["n"] == 1
    mock_log_exception.assert_called_once()
    # Restarts immediately on failure, not on the 24h aggregation cadence --
    # confirm whatever sleep duration is used is small (seconds, not a day).
    assert sleep_calls == [] or sleep_calls[0] < 3600


async def test_bus_collector_loop_does_not_poll_on_weekdays():
    call_count = {"n": 0}

    def should_not_be_called(should_continue=None):
        call_count["n"] += 1

    sleep_calls = []

    async def record_sleep_then_stop(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    with patch("app.main._is_weekend", return_value=False), \
         patch("app.main.run_forever", side_effect=should_not_be_called), \
         patch("app.main.asyncio.sleep", side_effect=record_sleep_then_stop):
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_collector_loop()

    assert call_count["n"] == 0
    # Re-checks hourly while paused, not on the 5s crash-restart cadence.
    assert sleep_calls == [3600]


async def test_bus_collector_loop_polls_on_weekends_passing_is_weekend_as_should_continue():
    received_should_continue = {}

    def capture_should_continue(should_continue=None):
        received_should_continue["fn"] = should_continue

    async def stop_after_one_cycle(seconds):
        raise asyncio.CancelledError()

    with patch("app.main._is_weekend", return_value=True) as mock_is_weekend, \
         patch("app.main.run_forever", side_effect=capture_should_continue), \
         patch("app.main.asyncio.sleep", side_effect=stop_after_one_cycle):
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_collector_loop()

    # run_forever must receive _is_weekend itself (so it can stop mid-poll
    # once the weekend ends), not a one-off boolean snapshot.
    assert received_should_continue["fn"] is mock_is_weekend


async def test_volume_check_loop_stops_collector_immediately_if_marker_already_exists(tmp_path):
    marker = tmp_path / ".bus_collection_complete"
    marker.write_text("{}")
    fake_task = MagicMock()

    with patch("app.main.BUS_COLLECTION_DONE_MARKER", marker):
        await _run_bus_volume_check_loop(fake_task)

    fake_task.cancel.assert_called_once()


async def test_volume_check_loop_keeps_polling_when_marker_does_not_exist(tmp_path):
    marker = tmp_path / ".bus_collection_complete"
    fake_task = MagicMock()
    sleep_calls = {"n": 0}

    async def sleep_once_then_stop(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:
            raise asyncio.CancelledError()

    with patch("app.main.BUS_COLLECTION_DONE_MARKER", marker), \
         patch("app.main.asyncio.sleep", side_effect=sleep_once_then_stop):
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_volume_check_loop(fake_task)

    fake_task.cancel.assert_not_called()
    assert sleep_calls["n"] == 2


async def test_volume_check_loop_stops_collector_as_soon_as_marker_appears_mid_poll(tmp_path):
    marker = tmp_path / ".bus_collection_complete"
    fake_task = MagicMock()

    async def sleep_then_create_marker(seconds):
        marker.write_text("{}")

    with patch("app.main.BUS_COLLECTION_DONE_MARKER", marker), \
         patch("app.main.asyncio.sleep", side_effect=sleep_then_create_marker):
        await _run_bus_volume_check_loop(fake_task)

    fake_task.cancel.assert_called_once()
