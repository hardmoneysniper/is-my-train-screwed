import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.api.chat import router as chat_router
from app.realtime_proxy import app as realtime_proxy_app, lifespan as realtime_proxy_lifespan, SUBWAY_ZIP, TripIndex
from app.trip_monitor import run_monitor_cycle
from collectors.bus_collector import run_forever
from db import get_connection
from scripts.aggregate_reliability_buckets import run_aggregate
from scripts.download_subwaydata import run_backfill as download_subwaydata_run_backfill
from scripts.ingest_subwaydata import (
    RAW_DIR as SUBWAY_RAW_DIR,
    run_ingest as ingest_subwaydata_run_ingest,
    build_static_stop_times_index,
)
from scripts.derive_bus_arrival_events import (
    RAW_DIR as BUS_RAW_DIR,
    run_derive as derive_bus_arrival_events_run_derive,
)

# Final whole-branch review, Minor #3: without an explicit basicConfig,
# logging.exception below relies on Python's last-resort handler, which is
# not guaranteed reliable output now that aggregation runs in-process
# (rather than as its own more visible service). One line makes failure
# logging dependable.
logging.basicConfig(level=logging.INFO)

# Task 10 brief: Railway can't share a volume across services, so the
# nightly aggregation can't be a separate cron service the way the plan
# originally described it (a cron service and this SQLite-backed backend
# would each get their own, disconnected copy of risk.sqlite3). It runs
# in-process instead, on the same schedule, sharing this process's file.
#
# Cost incident 2026-09-22: a real payment method is on file for this
# Railway account (contradicting the earlier-documented "no card, can't be
# charged" state -- that assumption is stale, see CLAUDE.md) and Railway
# bills GB-RAM-hours. Running hourly instead of daily means each cycle's
# idempotent download_subwaydata_run_backfill/ingest_subwaydata_run_ingest/
# derive_bus_arrival_events_run_derive calls process a much smaller
# incremental slice (new files since the last run) rather than one huge
# daily/startup batch -- lower peak memory and disk per cycle, spread out
# instead of spiked.
AGGREGATION_INTERVAL_S = 60 * 60

# Cost incident 2026-09-22 (continued): subway's TripIndex and 565K-row
# static stop-times index were previously cached permanently at module
# level, matching risk_engine.py's _default_route_index() convention --
# but that convention is for data queried on every live request, where the
# permanent RAM cost buys real latency savings. Ingestion runs briefly
# once an hour; the rest of the time, that cache bought nothing but a
# permanent increase in this always-on service's billed RAM footprint.
# Rebuilding fresh each cycle trades a bounded, short-lived CPU/memory
# spike (freed by GC immediately after the cycle) for eliminating that
# permanent tax.
def _run_aggregation_sync():
    # sqlite3 connections are single-thread-affine (check_same_thread
    # defaults to True) -- the connection must be opened AND used in the
    # same worker thread that asyncio.to_thread below runs this in, not
    # opened on the event-loop thread and passed in.
    conn = get_connection()
    try:
        # Each of these 3 steps is wrapped in its own try/except -- a
        # failure in one (e.g. a subway backfill network blip) must never
        # block its siblings or the existing run_aggregate() fold below
        # from still running in the same cycle.
        try:
            download_subwaydata_run_backfill(SUBWAY_RAW_DIR, datetime.now(timezone.utc).date())
        except Exception:
            logging.exception("subway backfill download failed")
        try:
            subway_trip_index = TripIndex(SUBWAY_ZIP)
            subway_static_index = build_static_stop_times_index(SUBWAY_ZIP)
            ingest_subwaydata_run_ingest(SUBWAY_RAW_DIR, conn, subway_trip_index, subway_static_index)
        except Exception:
            logging.exception("subway ingest failed")
        try:
            derive_bus_arrival_events_run_derive(BUS_RAW_DIR, conn)
        except Exception:
            logging.exception("bus derive failed")
        run_aggregate(conn)
    finally:
        conn.close()


async def _run_aggregation_loop():
    """Hourly fold of arrival_events into reliability_buckets (Task 5),
    run in-process (see module docstring). Also runs the subway
    backfill/ingest (subwaydata.nyc) and bus derive (Task 2) steps first,
    before the reliability_buckets fold, in the same cycle -- each call is
    idempotent (already-processed days/files are skipped), so hourly runs
    just mean smaller incremental slices instead of one big daily batch
    (see AGGREGATION_INTERVAL_S's cost-incident comment above). Runs
    immediately on startup -- not after waiting a full hour -- so a fresh
    deploy doesn't leave reliability_buckets empty. A failed run is caught
    and logged, never crashes the process -- the loop keeps going and
    retries on the next hourly cycle (same never-let-one-cycle-kill-the-
    loop philosophy as backup_from_railway.py's daemon loop).
    """
    while True:
        try:
            # Blocking sqlite I/O; hand it to a worker thread so a large
            # backlog fold doesn't stall /chat or /trip/plan requests
            # being served concurrently.
            await asyncio.to_thread(_run_aggregation_sync)
        except Exception:
            logging.exception("nightly aggregation run failed")
        await asyncio.sleep(AGGREGATION_INTERVAL_S)


# Phase 3 Task 7: the trip monitor poll loop (spec §6). Unlike
# _run_aggregation_sync above, this needs no asyncio.to_thread wrapping --
# that wrapping exists because run_aggregate is a fully synchronous,
# potentially long-running batch fold, whereas run_monitor_cycle is
# natively async (it awaits fetch_subway_alerts/fetch_bus_alerts/
# replan_trip directly) and its own sqlite calls are made the same
# un-wrapped way every other async endpoint in this codebase already calls
# sqlite (create_monitored_trip, cancel_monitored_trip, etc. -- none of
# Phase 2/3's async code paths wrap sqlite access in to_thread).
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


async def _run_bus_collector_loop():
    """run_forever() already retries internally on every transient
    failure (network errors, unexpected exceptions -- see its own
    backoff loop) and essentially never raises under normal operation.
    This wrapper exists for the one thing that CAN raise past it: a
    missing MTA_BUSTIME_API_KEY, checked once before its while True even
    starts. Restarts immediately (a short fixed delay, not the 24h
    aggregation cadence) since a crashed poller should come back fast."""
    while True:
        try:
            # Blocking, long-running I/O (its own internal time.sleep
            # polling loop) -- hand it to a worker thread so it never
            # stalls /chat or /trip/plan requests being served
            # concurrently, same reasoning as _run_aggregation_sync.
            await asyncio.to_thread(run_forever)
        except Exception:
            logging.exception("bus collector loop failed")
        await asyncio.sleep(BUS_COLLECTOR_RESTART_DELAY_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_run_aggregation_loop())
    monitor_task = asyncio.create_task(_run_monitor_loop())
    bus_collector_task = asyncio.create_task(_run_bus_collector_loop())
    # Mounting a sub-app (below) does not auto-trigger its own lifespan in
    # Starlette -- without driving it explicitly here, the proxy's
    # TripIndex (_trip_index) would stay None and every mounted
    # /proxy/rt/{feed_group} request would crash.
    async with realtime_proxy_lifespan(realtime_proxy_app):
        yield
    task.cancel()
    monitor_task.cancel()
    bus_collector_task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await bus_collector_task
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
