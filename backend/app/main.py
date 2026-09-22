import asyncio
import gzip
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.api.chat import router as chat_router
from app.day_type import day_type_for
from app.realtime_proxy import app as realtime_proxy_app, lifespan as realtime_proxy_lifespan
from app.trip_monitor import run_monitor_cycle
from collectors.bus_collector import CORRIDORS, DATA_DIR as BUS_RAW_DIR, run_forever
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
            # concurrently.
            await asyncio.to_thread(run_forever)
        except Exception:
            logging.exception("bus collector loop failed")
        await asyncio.sleep(BUS_COLLECTOR_RESTART_DELAY_S)


# Cost incident 2026-09-22 (continued): once each corridor of interest has
# "enough" data, collection should stop rather than keep polling MTA and
# billing Railway forever. CLAUDE.md's actual data-gating rule is n>=200
# per (route, stop, day_type) bucket, where day_type is just
# weekday/weekend (app/day_type.py) -- direction is not a separate axis
# in practice, since each physical stop_id already only ever appears with
# one direction. Computing real bucket occupancy precisely requires the
# full derive+aggregate step, which now runs locally
# (run_local_pipeline.py), not on this always-on service. Total raw
# records per route is a cheap proxy: computable directly from files
# already on disk, no parsing/bucketing needed.
#
# Per-route targets below are grounded in real observed data (2026-09-22,
# ~17.3h of fresh collection post-redeploy): distinct stop_id counts were
# M60+=35, Q70+=7, Q102=30. Per-day-type target = distinct_stops * 24
# hours * 200 (n-gate) * 1.5 (safety margin -- real traffic isn't evenly
# spread across hours, so the slowest/sparsest bucket takes noticeably
# longer than this average-case number to reach n=200; also a margin
# against undercounting stops from only ~17h of data, since rarely
# -visited stops may not have appeared yet). Recompute if CORRIDORS ever
# changes or a route's real stop count turns out very different from
# this session's measurement.
#
# Fixed 2026-09-22 (real bug, caught before it could bite): an earlier
# version of this check tracked one combined total per route, not split
# by day_type. Since weekday data accumulates ~5x faster than weekend
# (5 weekdays vs. 2 weekend days per week), a combined total could be
# satisfied almost entirely by weekday records while weekend buckets
# stayed empty -- and the collector would auto-stop with zero usable
# weekend reliability data. Tracking weekday and weekend independently
# closes that gap: both must independently cross their target.
BUS_COLLECTION_TARGETS = {
    "M60+": {"weekday": 250_000, "weekend": 250_000},
    "Q70+": {"weekday": 50_000, "weekend": 50_000},
    "Q102": {"weekday": 225_000, "weekend": 225_000},
}
BUS_COLLECTION_DONE_MARKER = Path(__file__).parent.parent / "data" / ".bus_collection_complete"
BUS_VOLUME_CHECK_INTERVAL_S = 24 * 60 * 60


def _bus_route_day_type_counts() -> dict[str, dict[str, int]]:
    """Per-route record counts, split by day_type (weekday/weekend, see
    app/day_type.py) -- the file's own date determines its day_type, so
    this reads the date once per file, not once per line."""
    counts = {route: {"weekday": 0, "weekend": 0} for route in CORRIDORS}
    for path in sorted(Path(BUS_RAW_DIR).glob("*.ndjson*")):
        service_date_str = path.name.split(".")[0]
        try:
            service_date = date.fromisoformat(service_date_str)
        except ValueError:
            continue
        day_type = day_type_for(service_date)
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt") as f:
                for line in f:
                    try:
                        route = json.loads(line).get("route_id")
                    except json.JSONDecodeError:
                        continue
                    if route in counts:
                        counts[route][day_type] += 1
        except OSError:
            continue
    return counts


async def _run_bus_volume_check_loop(bus_collector_task: "asyncio.Task") -> None:
    """Once every corridor in CORRIDORS crosses its BUS_COLLECTION_TARGETS
    entry for BOTH weekday and weekend, cancels bus_collector_task (no
    more MTA API polling, no more raw writes) and leaves a durable marker
    file so a later redeploy/restart doesn't silently resume collecting.
    Checked once a day, not on the collector's own 30s/5s cadences --
    this is a cheap proxy check, not something that needs tight latency.
    """
    if BUS_COLLECTION_DONE_MARKER.exists():
        bus_collector_task.cancel()
        return
    while True:
        await asyncio.sleep(BUS_VOLUME_CHECK_INTERVAL_S)
        try:
            counts = await asyncio.to_thread(_bus_route_day_type_counts)
        except Exception:
            logging.exception("bus volume check failed")
            continue
        target_reached = all(
            counts.get(route, {}).get(day_type, 0) >= target
            for route, targets_by_day_type in BUS_COLLECTION_TARGETS.items()
            for day_type, target in targets_by_day_type.items()
        )
        if target_reached:
            BUS_COLLECTION_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            BUS_COLLECTION_DONE_MARKER.write_text(json.dumps(counts))
            logging.info("bus collection target reached, stopping collector: %s", counts)
            bus_collector_task.cancel()
            return


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
