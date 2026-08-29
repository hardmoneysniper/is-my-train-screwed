"""backend/app/risk_engine.py

`get_risk(itinerary, conn=None) -> list[TransferRisk]` -- the core
deliverable of Phase 2 (see `.superpowers/sdd/task-6-brief.md`). Pure
function, no LLM calls: finds every transfer point in an `Itinerary`,
pulls the incoming-arrival and outgoing-headway distributions from
`reliability_buckets`, and Monte Carlo samples a miss probability. Never
fabricates a number -- CLAUDE.md's "LLM agents never compute numbers"
rule extends here too: any transfer whose required bucket is missing or
under n=200 gets `quality="insufficient"` and `p_miss=None`, with the
Monte Carlo never run.

Gap resolutions (see the brief for full rationale -- these are binding
decisions, not arbitrary choices made while coding):

- Gap 1 (stop_id feed prefix): `Leg.from_stop_id`/`to_stop_id` come from
  OTP as `"{feedId}:{entity_id}"`; `reliability_buckets.stop_id` is the
  bare id. Stripped via `_strip_feed_prefix` before every bucket lookup.

- Gap 2 (route_short_name != route_id): resolved via `RouteIndex`
  (`app/route_index.py`), built from the real static GTFS zips. A
  short_name matched by >1 route_id (e.g. subway's three "S" shuttles)
  is unmatchable -> `quality="insufficient"` for any transfer touching
  that leg, never a guess.

- Gap 3 (bus direction): `Leg` has no direction field. This session could
  not verify OTP's live GraphQL schema to check for a `directionId`
  field on `leg`/`leg.trip`, because Docker itself is not installed in
  this execution environment (`docker`/`docker compose` both resolve to
  "command not found" -- confirmed by trying, not assumed) -- there was
  no local OTP stack to introspect, so guessing the field from training
  data would have violated this project's "verify live, don't guess"
  discipline. Per the brief's own documented fallback: bus bucket
  lookups are direction-agnostic. `_fetch_bus_bucket` matches
  `(agency='bus', route_id, stop_id, day_type, hour_bucket, stat_type)`
  across BOTH direction rows stored in `reliability_buckets` and
  combines them by summing histograms/n_observations/n_ambiguous --
  more honest than guessing a direction. Subway is unaffected: its
  platform-level stop_ids already disambiguate direction (verified live
  in an earlier Phase 1 session), so subway lookups also skip the
  direction filter, but for the opposite reason -- see
  `_fetch_subway_bucket`'s docstring.
"""
import json
import pathlib
import random
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.day_type import day_type_for
from app.models.risk import TransferRisk
from app.models.transit import Itinerary, Leg
from app.route_index import RouteIndex
from db import get_connection

LOCAL_TZ = ZoneInfo("America/New_York")

MIN_N_OBSERVATIONS = 200
MONTE_CARLO_DRAWS = 1000
# Steady-state horizon of Task 5's decay recurrence (1 / (1 - 0.95) = 20):
# a bucket's effective sample never "remembers" more than this many days
# regardless of how long collection has run (task-6-brief.md step 6).
MAX_WINDOW_DAYS = 20

_MODE_TO_AGENCY = {"SUBWAY": "subway", "BUS": "bus"}

_GTFS_DIR = pathlib.Path(__file__).parent.parent / "data" / "gtfs"
_GTFS_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
]
_default_route_index_cache: RouteIndex | None = None


def _default_route_index() -> RouteIndex:
    """Lazily built, cached module-level RouteIndex from the real static
    GTFS zips (matches `realtime_proxy.py`'s TripIndex convention of
    loading a GTFS-derived index once and reusing it, not rebuilding it
    per call). Tests inject their own small `RouteIndex` instead of
    touching this."""
    global _default_route_index_cache
    if _default_route_index_cache is None:
        _default_route_index_cache = RouteIndex.from_gtfs(_GTFS_DIR / name for name in _GTFS_ZIP_NAMES)
    return _default_route_index_cache


def _strip_feed_prefix(stop_id: str) -> str:
    """OTP formats stop_id as "{feedId}:{entity_id}" (Gap 1). Buckets are
    keyed by the bare entity_id -- strip everything up to and including
    the first ':'. A stop_id with no ':' is returned unchanged (shouldn't
    happen for a real OTP leg, but never crash on it)."""
    _, sep, bare = stop_id.partition(":")
    return bare if sep else stop_id


def _local_day_type_and_hour(epoch_ms: int) -> tuple[str, int]:
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    dt_local = dt_utc.astimezone(LOCAL_TZ)
    return day_type_for(dt_local.date()), dt_local.hour


def _window_days(window_start_str: str) -> int:
    window_start = date.fromisoformat(window_start_str)
    elapsed = (date.today() - window_start).days
    return max(0, min(elapsed, MAX_WINDOW_DAYS))


def _fetch_subway_bucket(conn, route_id: str, stop_id: str, day_type: str, hour_bucket: int, stat_type: str) -> dict | None:
    """Subway bucket lookup: no direction filter (Gap 3) since a subway
    leg's platform-level stop_id already disambiguates direction. If more
    than one row matches this key -- meaning that assumption doesn't hold
    for this stop -- treat as unmatchable rather than silently picking
    one (task-6-brief.md Gap 3's explicit contingency)."""
    rows = conn.execute(
        """
        SELECT * FROM reliability_buckets
        WHERE agency = 'subway' AND route_id = ? AND stop_id = ?
          AND day_type = ? AND hour_bucket = ? AND stat_type = ?
        """,
        (route_id, stop_id, day_type, hour_bucket, stat_type),
    ).fetchall()
    if len(rows) != 1:
        return None
    return dict(rows[0])


def _combine_bus_rows(rows: list[dict]) -> dict:
    histograms = [json.loads(r["histogram"]) for r in rows]
    combined_counts = [sum(vals) for vals in zip(*(h["counts"] for h in histograms))]
    return {
        "histogram": json.dumps(
            {
                "bin_width_s": histograms[0]["bin_width_s"],
                "min_s": histograms[0]["min_s"],
                "counts": combined_counts,
            }
        ),
        "n_observations": sum(r["n_observations"] for r in rows),
        "n_ambiguous": sum(r["n_ambiguous"] for r in rows),
        # Conservative choice: the more-recently-started of the combined
        # direction buckets caps how far back the combined statistic can
        # honestly claim to reflect -- using the older one would overstate
        # confidence in data that (for the newer direction) doesn't exist.
        "window_start": max(r["window_start"] for r in rows),
    }


def _fetch_bus_bucket(conn, route_id: str, stop_id: str, day_type: str, hour_bucket: int, stat_type: str) -> dict | None:
    """Bus bucket lookup: direction-agnostic fallback (Gap 3 -- see module
    docstring). Combines both direction rows (if both exist) by summing
    their histograms/n_observations/n_ambiguous, rather than guessing
    which direction a leg is."""
    rows = conn.execute(
        """
        SELECT * FROM reliability_buckets
        WHERE agency = 'bus' AND route_id = ? AND stop_id = ?
          AND day_type = ? AND hour_bucket = ? AND stat_type = ?
        """,
        (route_id, stop_id, day_type, hour_bucket, stat_type),
    ).fetchall()
    if not rows:
        return None
    rows = [dict(r) for r in rows]
    if len(rows) == 1:
        return rows[0]
    return _combine_bus_rows(rows)


def _fetch_bucket(conn, agency: str, route_id: str, stop_id: str, day_type: str, hour_bucket: int, stat_type: str) -> dict | None:
    if agency == "subway":
        return _fetch_subway_bucket(conn, route_id, stop_id, day_type, hour_bucket, stat_type)
    if agency == "bus":
        return _fetch_bus_bucket(conn, route_id, stop_id, day_type, hour_bucket, stat_type)
    return None


def _bin_midpoints_and_weights(histogram: dict) -> tuple[list[float], list[float]]:
    counts = histogram["counts"]
    bin_width_s = histogram["bin_width_s"]
    min_s = histogram["min_s"]
    midpoints = [min_s + bin_width_s * (i + 0.5) for i in range(len(counts))]
    return midpoints, counts


def _monte_carlo_p_miss(incoming_histogram: dict, outgoing_histogram: dict, buffer_seconds: float, draws: int = MONTE_CARLO_DRAWS) -> float:
    """~1000-draw Monte Carlo (task-6-brief.md step 5 -- the controller's
    own resolution of an underspecified part of the spec, stated plainly
    for review, not an arbitrary choice). Each draw samples an incoming
    prediction-error offset and an outgoing headway independently from
    their respective histograms (weighted choice over bins by count,
    using each bin's midpoint). A draw counts as a miss only if the
    incoming leg blows the buffer AND the overrun isn't absorbed by a
    frequent-enough outgoing service."""
    incoming_midpoints, incoming_weights = _bin_midpoints_and_weights(incoming_histogram)
    outgoing_midpoints, outgoing_weights = _bin_midpoints_and_weights(outgoing_histogram)

    incoming_samples = random.choices(incoming_midpoints, weights=incoming_weights, k=draws)
    outgoing_samples = random.choices(outgoing_midpoints, weights=outgoing_weights, k=draws)

    misses = sum(
        1
        for incoming_offset, outgoing_headway in zip(incoming_samples, outgoing_samples)
        if incoming_offset > buffer_seconds and (incoming_offset - buffer_seconds) >= outgoing_headway
    )
    return misses / draws


def _compute_transfer_risk(
    conn: sqlite3.Connection,
    agency_i: str,
    route_id_i: str,
    stop_id_i: str,
    agency_j: str,
    route_id_j: str,
    stop_id_j: str,
    day_type: str,
    hour_bucket: int,
    buffer_seconds: float,
) -> tuple[float | None, float, int, str]:
    # Precompute lookup slot (deferred -- see docs/superpowers/plans/2026-08-27-phase-2-risk-engine-plan.md
    # Task 6 and README.md's "Known deferrals"). Not built yet: no 30+ days of real
    # data exist to run it against. When built, check a small (route_pair, day_type)
    # -> precomputed TransferRisk lookup here, before falling through to the live
    # Monte Carlo path below.

    incoming = _fetch_bucket(conn, agency_i, route_id_i, stop_id_i, day_type, hour_bucket, "prediction_error")
    outgoing = _fetch_bucket(conn, agency_j, route_id_j, stop_id_j, day_type, hour_bucket, "headway")

    incoming_n = incoming["n_observations"] if incoming else 0.0
    outgoing_n = outgoing["n_observations"] if outgoing else 0.0

    if incoming is None or outgoing is None or incoming_n < MIN_N_OBSERVATIONS or outgoing_n < MIN_N_OBSERVATIONS:
        n = min(incoming_n, outgoing_n)
        window_days_incoming = _window_days(incoming["window_start"]) if incoming else 0
        window_days_outgoing = _window_days(outgoing["window_start"]) if outgoing else 0
        return None, n, min(window_days_incoming, window_days_outgoing), "insufficient"

    incoming_histogram = json.loads(incoming["histogram"])
    outgoing_histogram = json.loads(outgoing["histogram"])
    p_miss = _monte_carlo_p_miss(incoming_histogram, outgoing_histogram, buffer_seconds)
    n = min(incoming_n, outgoing_n)
    window_days = min(_window_days(incoming["window_start"]), _window_days(outgoing["window_start"]))
    return p_miss, n, window_days, "ok"


def _transfer_risk_for_pair(conn: sqlite3.Connection, leg_i: Leg, leg_j: Leg, route_index: RouteIndex) -> TransferRisk:
    from_route = leg_i.route_short_name or ""
    to_route = leg_j.route_short_name or ""
    transfer_stop_name = leg_j.from_stop_name

    agency_i = _MODE_TO_AGENCY.get(leg_i.mode)
    agency_j = _MODE_TO_AGENCY.get(leg_j.mode)
    route_id_i = route_index.resolve(leg_i.route_short_name)
    route_id_j = route_index.resolve(leg_j.route_short_name)
    stop_id_i = _strip_feed_prefix(leg_i.to_stop_id) if leg_i.to_stop_id else None
    stop_id_j = _strip_feed_prefix(leg_j.from_stop_id) if leg_j.from_stop_id else None

    if None in (agency_i, agency_j, route_id_i, route_id_j, stop_id_i, stop_id_j):
        return TransferRisk(
            from_route=from_route,
            to_route=to_route,
            transfer_stop_name=transfer_stop_name,
            p_miss=None,
            n=0,
            window_days=0,
            quality="insufficient",
        )

    day_type, hour_bucket = _local_day_type_and_hour(leg_j.start_time_ms)
    buffer_seconds = (leg_j.start_time_ms - leg_i.end_time_ms) / 1000

    p_miss, n, window_days, quality = _compute_transfer_risk(
        conn, agency_i, route_id_i, stop_id_i, agency_j, route_id_j, stop_id_j,
        day_type, hour_bucket, buffer_seconds,
    )
    return TransferRisk(
        from_route=from_route,
        to_route=to_route,
        transfer_stop_name=transfer_stop_name,
        p_miss=p_miss,
        n=n,
        window_days=window_days,
        quality=quality,
    )


def get_risk(
    itinerary: Itinerary,
    conn: sqlite3.Connection | None = None,
    route_index: RouteIndex | None = None,
) -> list[TransferRisk]:
    """Pure function, no LLM calls. Returns one `TransferRisk` per transfer
    point in `itinerary`, in itinerary order -- `[]` for a single-leg or
    all-walk itinerary. `conn` defaults to `db.get_connection()` (closed
    here if we opened it); `route_index` defaults to a cached index built
    from the real static GTFS zips. Both are overridable so tests can
    inject a `tmp_path` DB and a small synthetic `RouteIndex` instead of
    the real ~150MB GTFS files (matches Tasks 1/3/4/5's testing
    convention)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    if route_index is None:
        route_index = _default_route_index()
    try:
        transit_legs = [leg for leg in itinerary.legs if leg.mode != "WALK"]
        return [
            _transfer_risk_for_pair(conn, leg_i, leg_j, route_index)
            for leg_i, leg_j in zip(transit_legs, transit_legs[1:])
        ]
    finally:
        if owns_conn:
            conn.close()
