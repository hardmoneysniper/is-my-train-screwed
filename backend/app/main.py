import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.api.chat import router as chat_router
from app.realtime_proxy import app as realtime_proxy_app, lifespan as realtime_proxy_lifespan
from app.trip_monitor import run_monitor_cycle
from collectors.bus_collector import run_forever
from db import get_connection

# Final whole-branch review, Minor #3: without an explicit basicConfig,
# logging.exception below relies on Python's last-resort handler, which is
# not guaranteed reliable output now that the trip monitor runs in-process
# (rather than as its own more visible service). One line makes failure
# logging dependable.
logging.basicConfig(level=logging.INFO)

# Cost incident 2026-09-22: a real payment method is on file for this
# Railway account (contradicting the earlier-documented "no card, can't be
# charged" state -- that assumption is stale, see CLAUDE.md) and Railway
# bills GB-RAM-hours. The subway backfill/ingest + reliability_buckets
# aggregation that previously ran here (Task 2 of the data-ingestion-
# pipeline plan) moved to a local, one-off script instead
# (scripts/run_local_pipeline.py) -- bus has no historical backfill
# source and genuinely needs a continuously-running collector, but subway
# does (subwaydata.nyc, any time, any machine) and reliability_buckets
# aggregation is a pure offline DB fold with zero live-service
# requirement. Running both on an always-on Railway service bought
# nothing but billed GB-RAM/disk-hours for work that doesn't need to be
# live. This service now runs ONLY the bus collector (below) and the
# trip monitor (Phase 3, a genuinely live feature) -- see CLAUDE.md's
# cost-incident section for the full writeup.

# Phase 3 Task 7: the trip monitor poll loop (spec §6). run_monitor_cycle
# is natively async (it awaits fetch_subway_alerts/fetch_bus_alerts/
# replan_trip directly) and its own sqlite calls are made the same
# un-wrapped way every other async endpoint in this codebase already calls
# sqlite (create_monitored_trip, cancel_monitored_trip, etc.).
MONITOR_INTERVAL_S = 60  # spec §6


async def _run_monitor_loop():
    while True:
        try:
            conn = get_connection()
            try:
                await run_monitor_cycle(conn)
            finally:
                conn.close()
        except Exception:
            logging.exception("trip monitor cycle failed")
        await asyncio.sleep(MONITOR_INTERVAL_S)


# Data-ingestion pipeline: the bus collector previously ran as its own
# standalone Railway service with its own volume -- but derive_bus_
# arrival_events.py needs its raw output on THIS service's volume, and
# Railway doesn't support sharing a volume across services (the same
# constraint that already forced the nightly aggregator in-process,
# above). Folding the collector in here means its own DATA_DIR (already
# resolved relative to wherever it runs) lands on this service's volume
# automatically, with zero change to the collector's own logic.
BUS_COLLECTOR_RESTART_DELAY_S = 5

# 2026-10-05 (user): every remaining corridor is already well past its
# weekday target (114%-293%) -- only weekend data is still needed for
# any of them. Collecting more weekday data now serves nobody, so the
# collector only runs on weekends; _is_weekend() checks UTC (matching
# how collected files get dated/bucketed elsewhere in this project --
# _rotated_path uses datetime.now(timezone.utc), and day_type_for buckets
# by that same date, so using UTC here keeps "is this weekend data"
# consistent with how it's later bucketed, not local machine time).
BUS_COLLECTOR_WEEKDAY_CHECK_INTERVAL_S = 60 * 60


def _is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5  # Sat=5, Sun=6


async def _run_bus_collector_loop():
    """run_forever() already retries internally on every transient
    failure (network errors, unexpected exceptions -- see its own
    backoff loop) and essentially never raises under normal operation.
    This wrapper exists for two things that make it return/raise: a
    missing MTA_BUSTIME_API_KEY (raises, checked once before its while
    True even starts) and _is_weekend() turning False mid-poll (returns
    cleanly, checked once per ~60s poll cycle inside run_forever itself).
    While it's not weekend, this loop doesn't even call run_forever --
    it just re-checks hourly. Restarts/resumes with a short fixed delay
    (not the 24h aggregation cadence) since both a crashed poller and a
    freshly-started weekend should come back fast."""
    while True:
        if not _is_weekend():
            await asyncio.sleep(BUS_COLLECTOR_WEEKDAY_CHECK_INTERVAL_S)
            continue
        try:
            # Blocking, long-running I/O (its own internal time.sleep
            # polling loop) -- hand it to a worker thread so it never
            # stalls /chat or /trip/plan requests being served
            # concurrently.
            await asyncio.to_thread(run_forever, _is_weekend)
        except Exception:
            logging.exception("bus collector loop failed")
        await asyncio.sleep(BUS_COLLECTOR_RESTART_DELAY_S)


# Cost incident 2026-09-22 (continued), fixed 2026-09-28: once each
# corridor of interest has "enough" data, collection should stop rather
# than keep polling MTA and billing Railway forever. This used to be
# computed HERE by scanning BUS_RAW_DIR for cumulative per-route,
# per-day-type record counts -- but that was a real, silent bug: the
# hourly local sync daemon (scripts/hourly_bus_sync.py) deletes every
# raw file from this same directory shortly after pulling it, so this
# service's own view of "how much have we collected" was being reset to
# near-zero every hour and could never reach any real cumulative target.
# The local machine has the only durable, undeleted view of cumulative
# collection (nothing is ever deleted there) -- so the target-check and
# BUS_COLLECTION_TARGETS values now live in hourly_bus_sync.py, which
# writes BUS_COLLECTION_DONE_MARKER here (via railway ssh) once it
# determines targets are met. This loop's only job is to notice that
# marker and stop the collector -- it holds no target data of its own.
BUS_COLLECTION_DONE_MARKER = Path(__file__).parent.parent / "data" / ".bus_collection_complete"
BUS_MARKER_CHECK_INTERVAL_S = 60 * 60  # matches hourly_bus_sync.py's own cadence --
# checking more often than the marker could possibly appear wastes cycles, checking
# less often would leave the collector running for up to a day past target.


async def _run_bus_volume_check_loop(bus_collector_task: "asyncio.Task") -> None:
    """Watches for BUS_COLLECTION_DONE_MARKER and cancels bus_collector_task
    (no more MTA API polling, no more raw writes) as soon as it appears --
    written by hourly_bus_sync.py once it determines every corridor has
    crossed its per-day-type target from the durable local data. Checking
    at startup too, not just after the first sleep, so a redeploy after
    the marker was already written doesn't silently resume collecting.
    """
    while True:
        if BUS_COLLECTION_DONE_MARKER.exists():
            logging.info("bus collection marker found, stopping collector")
            bus_collector_task.cancel()
            return
        await asyncio.sleep(BUS_MARKER_CHECK_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(_run_monitor_loop())
    bus_collector_task = asyncio.create_task(_run_bus_collector_loop())
    bus_volume_check_task = asyncio.create_task(_run_bus_volume_check_loop(bus_collector_task))
    # Mounting a sub-app (below) does not auto-trigger its own lifespan in
    # Starlette -- without driving it explicitly here, the proxy's
    # TripIndex (_trip_index) would stay None and every mounted
    # /proxy/rt/{feed_group} request would crash.
    async with realtime_proxy_lifespan(realtime_proxy_app):
        yield
    monitor_task.cancel()
    bus_collector_task.cancel()
    bus_volume_check_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await bus_collector_task
    except asyncio.CancelledError:
        pass
    try:
        await bus_volume_check_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Is My Train Screwed?", lifespan=lifespan)
app.include_router(trip_router)
app.include_router(chat_router)
# Mounts the subway GTFS-RT trip-id rewriting proxy (Phase 1 Task 11) at
# /proxy so one Railway service can serve both /chat and /proxy/rt/... --
# avoiding a 4th service that would also need its own copy of subway.zip
# for TripIndex. realtime_proxy.py still runs standalone unchanged
# (`uvicorn app.realtime_proxy:app`) for local dev/tests that use it that
# way -- mounting the same `app` object here doesn't preclude that.
app.mount("/proxy", realtime_proxy_app)


@app.get("/health")
def health():
    return {"status": "ok"}
