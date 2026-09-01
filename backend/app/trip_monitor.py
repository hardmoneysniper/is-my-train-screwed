"""backend/app/trip_monitor.py

`run_monitor_cycle(conn=None, route_index=None) -> dict` -- Phase 3 Task 7's
poll loop. Runs once per cycle (driven by `app/main.py`'s
`_run_monitor_loop`, every `MONITOR_INTERVAL_S`): claims due active trips
(Task 1's atomic `claim_active_trips_for_polling`), sweeps any that have
passed their TTL, checks the rest against the live MTA alert feeds (Task
3), and calls Task 6's `replan_trip` for any trip whose route is touched
by an active alert.

Deliberately deferred, NOT built here: the design doc's "headway-anomaly
check against Phase 2's own `reliability_buckets`". A real version of that
check requires comparing *live, current* vehicle spacing at a stop against
the historical `headway` distribution -- but no existing capability in
this codebase exposes "what's the current live headway at stop X" as a
queryable function (`realtime_proxy.py` only re-serves protobuf to OTP; no
standalone live-headway computation exists from Phase 1 or 2). Building
that from scratch would be a large, unplanned expansion of this task's
scope, and none of this task's required tests exercise it -- same
reasoning as Task 6's own deliberate deferral of the Haiku
multi-route-comparison path, and this project's hard rule against faking
missing capability (`CLAUDE.md`: "if something's missing, build what
doesn't depend on it and flag what's blocked -- don't fake it"). This
module triggers re-plans from the alerts feed and sweeps TTL only.
"""
import logging
import sqlite3
from datetime import datetime, timezone

from app import risk_engine
from app.agents.replan_agent import replan_trip
from app.alerts import fetch_bus_alerts, fetch_subway_alerts
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary
from app.route_index import RouteIndex
from db import claim_active_trips_for_polling, get_connection

# Matches this loop's own poll interval (app/main.py's MONITOR_INTERVAL_S)
# -- a trip becomes eligible for re-check again exactly one interval after
# its last check, never sooner (would re-check pointlessly) and never much
# later (would delay noticing a real problem).
STALENESS_SECONDS = 60


def _trip_route_ids(trip: MonitoredTrip, route_index: RouteIndex) -> set[str]:
    """The set of real route_ids (never short names) touched by `trip`'s
    transit legs, resolved via RouteIndex so it can be intersected against
    an AlertRecord's own route_ids (also real route_ids, straight off the
    GTFS-RT feed). WALK legs have no route and are skipped; an unresolvable
    or ambiguous short_name (RouteIndex.resolve returning None) is dropped
    rather than guessed."""
    ids = {route_index.resolve(leg.route_short_name) for leg in trip.itinerary_snapshot.legs if leg.mode != "WALK"}
    return {i for i in ids if i is not None}


async def run_monitor_cycle(conn: sqlite3.Connection | None = None, route_index: RouteIndex | None = None) -> dict:
    """One poll cycle. Same `owns_conn` pattern as every other Phase 2/3
    entry point (`risk_engine.get_risk`, `replan_agent.replan_trip`): opens
    one connection if `conn` is `None`, closes it in `finally` if opened.
    This single connection is used for the claim, every per-trip write,
    and is passed as `conn=conn` into every `replan_trip(...)` call this
    cycle -- matching `replan_trip`'s own documented expectation that Task
    7's poll loop passes its own per-trip transaction connection.

    `route_index` defaults to `risk_engine._default_route_index()` (the
    same cached, real-GTFS-backed singleton `risk_engine.py`/`deadline.py`
    already share) unless injected -- same convention as
    `get_risk`/`compute_deadline_threshold`.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        claimed_rows = claim_active_trips_for_polling(conn, staleness_seconds=STALENESS_SECONDS)
        claimed_trips = []
        for row in claimed_rows:
            row = dict(row)
            row["itinerary_snapshot"] = Itinerary.model_validate_json(row["itinerary_snapshot"])
            claimed_trips.append(MonitoredTrip.model_validate(row))

        if not claimed_trips:
            summary = {
                "trips_claimed": 0,
                "trips_expired": 0,
                "trips_replanned": 0,
                "trips_failed": 0,
            }
            print(f"[trip_monitor] {summary}")
            return summary

        # Fetched once per cycle regardless of how many trips were claimed
        # -- each feed's failure is caught independently so a subway feed
        # outage never also blocks bus-trip checks (or vice versa). A
        # failed feed degrades to an empty list for this cycle only: log
        # and skip, never fabricate an alert, never crash the cycle.
        try:
            subway_alerts = await fetch_subway_alerts()
        except Exception:
            logging.exception("trip_monitor: subway alerts fetch failed, skipping subway alert checks this cycle")
            subway_alerts = []
        try:
            bus_alerts = await fetch_bus_alerts()
        except Exception:
            logging.exception("trip_monitor: bus alerts fetch failed, skipping bus alert checks this cycle")
            bus_alerts = []
        all_alerts = [a for a in subway_alerts + bus_alerts if a.active]

        if route_index is None:
            route_index = risk_engine._default_route_index()

        now = datetime.now(timezone.utc)
        trips_expired = trips_replanned = trips_failed = 0
        for trip in claimed_trips:
            try:
                if now > trip.ttl_expires_at:
                    # Expiry is silent cleanup, no notification (spec §6)
                    # -- checked BEFORE the alert check so an expired trip
                    # short-circuits even if it also has a matching alert.
                    conn.execute("UPDATE monitored_trips SET status = 'expired' WHERE id = ?", (trip.id,))
                    conn.commit()
                    trips_expired += 1
                    continue

                trip_route_ids = _trip_route_ids(trip, route_index)
                matched_alert = next((a for a in all_alerts if trip_route_ids & set(a.route_ids)), None)
                if matched_alert is not None:
                    result = await replan_trip(trip, f"alert: {matched_alert.header_text}", conn=conn)
                    if result is not None:
                        trips_replanned += 1
                # A trip with no matching alert simply falls through --
                # last_checked_at was already updated atomically by the
                # claim above, nothing further to do for a healthy trip.
            except Exception:
                # One trip's failure (a network error inside replan_trip, a
                # malformed itinerary snapshot, anything) must not stop the
                # loop from checking the rest of this cycle's claimed trips.
                logging.exception(f"trip_monitor: failed to process trip {trip.id}")
                trips_failed += 1

        summary = {
            "trips_claimed": len(claimed_trips),
            "trips_expired": trips_expired,
            "trips_replanned": trips_replanned,
            "trips_failed": trips_failed,
        }
        print(f"[trip_monitor] {summary}")
        return summary
    finally:
        if owns_conn:
            conn.close()
