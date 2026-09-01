"""backend/app/deadline.py

`compute_deadline_threshold(itinerary, deadline_ts, conn=None,
route_index=None) -> int | None` -- Phase 3 Task 4's deadline-mode
backward-planning helper (see `.superpowers/sdd/task-4-brief.md`). Pure
function, no LLM calls, same discipline as `risk_engine.get_risk`: given
an `Itinerary` and a deadline (epoch-ms), computes the latest safe
departure epoch-ms timestamp using each transit leg's own real,
historical p85 arrival-deviation from `reliability_buckets` -- never a
guessed or hardcoded number.

This module deliberately reuses `risk_engine`'s private helpers
(`_incoming_stat_type`, `_strip_feed_prefix`, `_local_day_type_and_hour`,
`_fetch_bucket`, `_MODE_TO_AGENCY`, `MIN_N_OBSERVATIONS`) rather than
duplicating their logic -- see the brief's Question 1 for why "which
stat_type reflects this agency's arrival-time spread" is the same
question `get_risk` already answers for a transfer's incoming leg, just
asked here for every transit leg instead of only transfer-incoming ones.

Variance across legs is combined by summing each leg's own conservative
p85 deviation (a deterministic closed-form sum, not a second Monte
Carlo) -- see the brief's Question 2. This is a real, honest
overestimate (never an underestimate) of the itinerary's true p85
lateness, which is the safe direction for a feature whose purpose is
warning someone before they miss a deadline.

Failure mode is all-or-nothing: if ANY transit leg lacks a matching
bucket, or that bucket's n_observations < MIN_N_OBSERVATIONS, the whole
function returns None -- never silently treats a no-data leg as
contributing zero deviation (that would understate the buffer, the wrong
failure direction for this feature).
"""
import json
import sqlite3

from app import risk_engine
from app.models.transit import Itinerary
from app.route_index import RouteIndex


def _p85_seconds(histogram: dict) -> float:
    """The bucket's 85th-percentile arrival deviation, in seconds, using
    each qualifying bin's UPPER edge (conservative: "at least 85% of
    observations fall at or below this value" uses the worst case within
    the qualifying bin, not its midpoint). See task-4-brief.md's worked
    example for a fully hand-computed walkthrough."""
    counts = histogram["counts"]
    bin_width_s = histogram["bin_width_s"]
    min_s = histogram["min_s"]
    total = sum(counts)  # exactly equals the bucket's n_observations by
                          # construction -- the aggregator decays histogram
                          # counts and n_observations with identical
                          # DECAY_OLD/DECAY_NEW weights from identical
                          # starting values (verified directly against
                          # aggregate_reliability_buckets.py's _upsert_bucket)
    threshold = 0.85 * total
    cumulative = 0.0
    for i, c in enumerate(counts):
        cumulative += c
        if cumulative >= threshold:
            return min_s + bin_width_s * (i + 1)  # bin's UPPER edge
    return min_s + bin_width_s * len(counts)  # unreachable if total > 0 and
                                                # threshold <= total, kept
                                                # only as a defensive floor


def compute_deadline_threshold(
    itinerary: Itinerary,
    deadline_ts: int,
    conn: sqlite3.Connection | None = None,
    route_index: RouteIndex | None = None,
) -> int | None:
    """Latest safe departure epoch-ms timestamp for `itinerary` to reach
    `deadline_ts` on time, using real historical p85 lateness per transit
    leg -- or None if any transit leg's data is insufficient (all-or-
    nothing, see module docstring).

    `deadline_ts` and the return value are both epoch-ms `int` -- this
    function stays at the itinerary-math boundary like `risk_engine`'s
    raw-epoch-ms `Leg` handling, never `datetime`. Converting a stored
    `MonitoredTrip.deadline_ts: datetime` to epoch-ms is the caller's job
    (Task 5's `create_monitored_trip`, not built here).

    `conn` defaults to `db.get_connection()` (closed here if we opened
    it); `route_index` defaults to `risk_engine`'s cached real-GTFS
    index. Both overridable so tests can inject a tmp_path DB and a small
    synthetic RouteIndex, matching `get_risk`'s own convention.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = risk_engine.get_connection()
    if route_index is None:
        route_index = risk_engine._default_route_index()
    try:
        total_p85_buffer_seconds = 0.0
        for leg in itinerary.legs:
            if leg.mode == "WALK":
                continue

            agency = risk_engine._MODE_TO_AGENCY.get(leg.mode)
            route_id = route_index.resolve(leg.route_short_name)
            stop_id = risk_engine._strip_feed_prefix(leg.to_stop_id) if leg.to_stop_id else None

            if agency is None or route_id is None or stop_id is None:
                return None

            day_type, hour_bucket = risk_engine._local_day_type_and_hour(leg.end_time_ms)
            stat_type = risk_engine._incoming_stat_type(agency)
            bucket = risk_engine._fetch_bucket(conn, agency, route_id, stop_id, day_type, hour_bucket, stat_type)

            if bucket is None or bucket["n_observations"] < risk_engine.MIN_N_OBSERVATIONS:
                return None

            histogram = json.loads(bucket["histogram"])
            total_p85_buffer_seconds += _p85_seconds(histogram)

        p85_travel_time_ms = itinerary.duration_seconds * 1000 + total_p85_buffer_seconds * 1000
        return deadline_ts - round(p85_travel_time_ms)
    finally:
        if owns_conn:
            conn.close()
