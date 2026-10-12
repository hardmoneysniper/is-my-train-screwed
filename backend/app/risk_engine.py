"""backend/app/risk_engine.py

`get_risk(itinerary, conn=None) -> list[TransferRisk]` -- the core
deliverable of Phase 2 (see `.superpowers/sdd/task-6-brief.md`). Pure
function, no LLM calls: finds every transfer point in an `Itinerary`,
pulls the incoming-arrival and outgoing-headway distributions from
`reliability_buckets`, and computes a miss probability exactly from the
real observed data (see _p_miss_from_incoming's docstring for the
2026-10-12 redesign). Never fabricates a number -- CLAUDE.md's "LLM
agents never compute numbers" rule extends here too: a bucket missing or
under MIN_N_OBSERVATIONS gets `quality="insufficient"` and `p_miss=None`,
computed from nothing.

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
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import bus_schedule_periods
from app.day_type import day_type_for
from app.models.risk import TransferRisk
from app.models.transit import Itinerary, Leg
from app.route_index import RouteIndex
from db import get_connection

LOCAL_TZ = ZoneInfo("America/New_York")

MIN_N_OBSERVATIONS = 50
# 2026-10-12 (user): lowered from 200. With p_miss now a single-
# distribution exact calculation (not a compound two-distribution Monte
# Carlo estimate), 200 real observations isn't needed for a defensible
# percentage -- n=50 still reliably surfaces a 5%+ true miss rate (~92%
# chance of seeing at least one such event in 50 real trials:
# 1 - 0.95**50 ≈ 0.92), while being achievable in weeks rather than
# months for lower-frequency routes/hours. The underlying histograms are
# still built entirely from real observed incoming/outgoing trips --
# lowering the threshold doesn't change what's real about the data, only
# how much of it is required before citing a number from it.
# Freshness cap for citations (task-6-brief.md step 6): a transfer-risk
# citation never claims to reflect more than this many days of history,
# even once real collection has run far longer -- transit patterns and
# schedules can shift, so capping claimed recency is a deliberate
# honesty choice, independent of how n_observations itself is computed.
# (Originally derived from the aggregator's now-removed decay formula's
# memory horizon, 1 / (1 - 0.95) = 20 -- that formula is gone (2026-10-11,
# see aggregate_reliability_buckets.py's module docstring), but 20 days
# remains a reasonable freshness cap on its own terms, so the value is
# kept, just no longer tied to decay math.)
MAX_WINDOW_DAYS = 20

_MODE_TO_AGENCY = {
    "SUBWAY": "subway",
    "BUS": "bus",
    "FERRY": "ferry",
    # 2 of NYC Ferry's 9 real GTFS routes (RES "Rockaway East", RWS
    # "Rockaway West") are route_type=3 (bus) in the source feed, not
    # route_type=4 (ferry) -- a real shuttle-bus segment bundled into the
    # ferry feed, not a data error. OTP tags those legs mode="BUS", so
    # they resolve via the "BUS" entry above, not this one -- correct
    # given the source data, not a gap to special-case.
}

_GTFS_DIR = pathlib.Path(__file__).parent.parent / "data" / "gtfs"
_GTFS_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
    "ferry.zip",
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


def _p_miss_from_incoming(incoming_histogram: dict, buffer_seconds: float) -> float:
    """2026-10-12 redesign (user-directed): a miss is now defined purely as
    "did the incoming leg run late enough that you arrived after your
    planned connection already departed" -- i.e. incoming_offset >
    buffer_seconds, nothing more. The previous version also required the
    overrun to exceed the outgoing route's typical headway (reasoning:
    missing one bus barely matters if the next one comes in 3 minutes) --
    but that silently baked a judgment call ("does missing it matter?")
    into the cited number itself, and coupled the citation to TWO
    independent buckets instead of one. Splitting them: this function
    answers the narrow, honest question ("did you catch the one OTP
    planned for you") from the incoming bucket alone; _typical_wait_seconds
    below answers the separate, independently-gated question ("how long do
    you wait if you didn't"), which the Conversation Agent is instructed to
    always state alongside this percentage, never in place of it.

    Computed exactly over the full histogram (sum of bin weight past the
    buffer, divided by total weight) rather than Monte Carlo sampled --
    a single-distribution threshold probability has a closed form, no
    need to introduce sampling noise into a cited number."""
    midpoints, weights = _bin_midpoints_and_weights(incoming_histogram)
    total = sum(weights)
    miss_weight = sum(w for m, w in zip(midpoints, weights) if m > buffer_seconds)
    return miss_weight / total


def _typical_wait_seconds(outgoing_histogram: dict) -> float:
    """Weighted mean of the outgoing route's headway histogram -- "if you
    miss the planned vehicle, how long do you typically wait for the next
    one." Independently gated on the outgoing bucket's own n_observations
    (see _compute_transfer_risk), separate from whether the incoming
    bucket is sufficient for a miss percentage."""
    midpoints, weights = _bin_midpoints_and_weights(outgoing_histogram)
    return sum(m * w for m, w in zip(midpoints, weights)) / sum(weights)


def _incoming_stat_type(agency_i: str) -> str:
    """Which stat_type the incoming leg's bucket is fetched from -- this is
    the fix for a real cross-task gap (final whole-branch review, Critical
    finding): Task 3 (subway ingestion) hardcodes
    `predicted_arrival_ts_at_T_minus_5 = None` for every subway event
    (correct -- subwaydata.nyc has no such field, and there's no live
    subway GTFS-RT collector), which means the aggregator (Task 5) never
    creates a `prediction_error` bucket for `agency='subway'`, ever. Task 6
    originally hardcoded `"prediction_error"` for the incoming leg
    regardless of agency -- individually reasonable, but the two decisions
    don't compose: every subway-incoming transfer was permanently stuck at
    `quality="insufficient"`, including this product's own beachhead
    Roosevelt Island F->Q transfer.

    Subway *does* have real, populated data for a different stat --
    `delay_seconds` (observed vs. static schedule), folded into `delay`
    buckets -- so subway's incoming leg reads `"delay"` instead. Bus is
    unaffected: Task 4 deliberately leaves `delay_seconds` NULL for every
    bus event (no static-schedule matching was in scope), so bus has no
    `delay` buckets at all -- `"prediction_error"` remains bus's only real
    incoming-spread source and stays the primary (and only) choice for it.
    Both stats share the identical histogram shape (30s bins, -10min to
    +40min, task-5-brief.md), so no change to the Monte Carlo sampling
    itself is needed -- only which bucket gets fetched."""
    return "delay" if agency_i == "subway" else "prediction_error"


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
) -> tuple[float | None, float, int, str, float | None, bool]:
    # Precompute lookup slot (deferred -- see docs/superpowers/plans/2026-08-27-phase-2-risk-engine-plan.md
    # Task 6 and README.md's "Known deferrals"). Not built yet: no 30+ days of real
    # data exist to run it against. When built, check a small (route_pair, day_type)
    # -> precomputed TransferRisk lookup here, before falling through to the live
    # path below.

    # 2026-10-12 (user): bus buckets are keyed by real scheduled-headway
    # period, not literal hour-of-day, AND weekend is folded into weekday
    # wherever a route's real schedule doesn't distinguish them (see
    # app/bus_schedule_periods.py and aggregate_reliability_buckets.py's
    # _effective_day_type_and_hour, which wrote them this way) -- a
    # bus-side lookup must map through the SAME function per route (each
    # route's equivalence pattern is its own) or it will miss real
    # buckets. Subway is unaffected, same literal day_type/hour_bucket as
    # always.
    day_type_i, hour_bucket_i = (
        bus_schedule_periods.effective_day_type_and_hour(route_id_i, day_type, hour_bucket)
        if agency_i == "bus" else (day_type, hour_bucket)
    )
    day_type_j, hour_bucket_j = (
        bus_schedule_periods.effective_day_type_and_hour(route_id_j, day_type, hour_bucket)
        if agency_j == "bus" else (day_type, hour_bucket)
    )

    incoming = _fetch_bucket(conn, agency_i, route_id_i, stop_id_i, day_type_i, hour_bucket_i, _incoming_stat_type(agency_i))
    outgoing = _fetch_bucket(conn, agency_j, route_id_j, stop_id_j, day_type_j, hour_bucket_j, "headway")

    # 2026-10-12 redesign: the miss percentage (quality/p_miss/n/window_days)
    # and the wait-time/reroute-offer signal are independently gated, each
    # on its own single bucket -- a citable percentage no longer requires
    # the outgoing bucket too, and vice versa (see _p_miss_from_incoming's
    # docstring for why).
    incoming_n = incoming["n_observations"] if incoming else 0.0
    n = incoming_n
    window_days = _window_days(incoming["window_start"]) if incoming else 0
    p_miss: float | None = None
    quality = "insufficient"
    if incoming is not None and incoming_n >= MIN_N_OBSERVATIONS:
        incoming_histogram = json.loads(incoming["histogram"])
        # Cheap insurance (final whole-branch review, Minor #2, still
        # applies): an all-zero histogram is just as "no real signal" as
        # a missing bucket, so degrade the same way rather than divide by
        # zero in _p_miss_from_incoming.
        if sum(incoming_histogram["counts"]) > 0:
            p_miss = _p_miss_from_incoming(incoming_histogram, buffer_seconds)
            quality = "ok"

    # 2026-10-12 (user): no magnitude threshold on the wait itself -- "avoid
    # the gate at all". Whenever a real typical_wait_seconds is citable, the
    # agent always pairs it with the miss percentage and always offers a
    # faster reroute, letting the rider -- not a hardcoded minute cutoff --
    # decide whether the wait is worth it.
    outgoing_n = outgoing["n_observations"] if outgoing else 0.0
    typical_wait_seconds: float | None = None
    should_offer_reroute = False
    if outgoing is not None and outgoing_n >= MIN_N_OBSERVATIONS:
        outgoing_histogram = json.loads(outgoing["histogram"])
        if sum(outgoing_histogram["counts"]) > 0:
            typical_wait_seconds = _typical_wait_seconds(outgoing_histogram)
            should_offer_reroute = True

    return p_miss, n, window_days, quality, typical_wait_seconds, should_offer_reroute


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

    p_miss, n, window_days, quality, typical_wait_seconds, should_offer_reroute = _compute_transfer_risk(
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
        typical_wait_seconds=typical_wait_seconds,
        should_offer_reroute=should_offer_reroute,
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
    convention). No rush/non-rush distinction (2026-10-12, user: "avoid the
    gate at all") -- should_offer_reroute is simply whether a citable
    typical_wait_seconds exists; the agent always narrates it alongside
    p_miss and always asks whether the rider wants a faster route, with no
    hardcoded minute threshold deciding that for them."""
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
