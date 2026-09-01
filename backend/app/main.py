import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.api.chat import router as chat_router
from app.realtime_proxy import app as realtime_proxy_app, lifespan as realtime_proxy_lifespan
from app.trip_monitor import run_monitor_cycle
from db import get_connection
from scripts.aggregate_reliability_buckets import run_aggregate

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
AGGREGATION_INTERVAL_S = 24 * 60 * 60


def _run_aggregation_sync():
    # sqlite3 connections are single-thread-affine (check_same_thread
    # defaults to True) -- the connection must be opened AND used in the
    # same worker thread that asyncio.to_thread below runs this in, not
    # opened on the event-loop thread and passed in.
    conn = get_connection()
    try:
        run_aggregate(conn)
    finally:
        conn.close()


async def _run_aggregation_loop():
    """Nightly fold of arrival_events into reliability_buckets (Task 5),
    run in-process (see module docstring). Runs immediately on startup --
    not after waiting a full 24h -- so a fresh deploy doesn't leave
    reliability_buckets empty for a day; Task 5's design already folds a
    large initial backlog in one run. A failed run is caught and logged,
    never crashes the process -- the loop keeps going and retries on the
    next 24h cycle (same never-let-one-cycle-kill-the-loop philosophy as
    backup_from_railway.py's daemon loop).
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_run_aggregation_loop())
    monitor_task = asyncio.create_task(_run_monitor_loop())
    # Mounting a sub-app (below) does not auto-trigger its own lifespan in
    # Starlette -- without driving it explicitly here, the proxy's
    # TripIndex (_trip_index) would stay None and every mounted
    # /proxy/rt/{feed_group} request would crash.
    async with realtime_proxy_lifespan(realtime_proxy_app):
        yield
    task.cancel()
    monitor_task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await monitor_task
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
