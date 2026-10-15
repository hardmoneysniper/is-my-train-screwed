"""Pydantic models for the Phase 2 risk-engine sqlite schema (spec §5.2).

These mirror `arrival_events` / `reliability_buckets` in `backend/db.py`
field-for-field. Per Plan Decision 1 ("raw sqlite3, no ORM; Pydantic
models at the read/write boundary"), these are not an ORM layer -- they
exist so ingestion/aggregation/query code (Tasks 2-6) can validate rows
in and out of sqlite3.Row dicts instead of passing bare tuples/dicts
around.
"""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

DayType = Literal["weekday", "weekend"]


class ArrivalEvent(BaseModel):
    """One observed vehicle passage at a stop."""

    id: int | None = None
    agency: str
    route_id: str
    direction: str
    stop_id: str
    vehicle_id: str
    trip_id: str | None = None
    observed_arrival_ts: datetime
    scheduled_arrival_ts: datetime | None = None
    delay_seconds: int | None = None
    predicted_arrival_ts_at_T_minus_5: datetime | None = None
    service_date: date
    day_type: DayType
    hour_bucket: int
    derivation_quality: Literal["clean", "interpolated", "ambiguous"]


class ReliabilityBucket(BaseModel):
    """One aggregate bucket -- the table query-time (`get_risk`) reads."""

    id: int | None = None
    agency: str
    route_id: str
    stop_id: str
    direction: str
    day_type: DayType
    hour_bucket: int
    stat_type: Literal["headway", "delay", "prediction_error"]
    histogram: dict
    # float, not int: SQLite's INT column type doesn't enforce the Python
    # type back out, and histogram["counts"] entries are floats by
    # construction (bin accumulation uses += 1.0) -- kept float for
    # consistency with those, though a cumulative sum of integer event
    # counts is always whole-valued in practice.
    n_observations: float
    n_ambiguous: float
    window_start: date
    last_updated: datetime


class TransferRisk(BaseModel):
    """Per-transfer result from `get_risk` (Task 6) -- one entry per
    transfer point in an itinerary, in itinerary order.

    2026-10-12 redesign (user-directed): `p_miss` and `typical_wait_seconds`
    are two separate, INDEPENDENTLY-gated signals, not one compound number:

    - `p_miss` answers "did you catch the specific vehicle OTP planned for
      you" -- computed purely from the INCOMING leg's delay/prediction-error
      distribution vs. the planned transfer buffer. Gated on a Wilson score
      confidence interval over the incoming bucket's own histogram weights
      (2026-10-15, replacing a flat `n >= MIN_N_OBSERVATIONS` floor -- see
      `risk_engine.P_MISS_CI_HALF_WIDTH_MAX`'s docstring): citable once the
      interval is narrow enough, regardless of raw n -- a route whose true
      miss rate sits confidently near 0% or 100% needs far fewer real
      samples than a genuinely uncertain ~50/50 case. `quality == "ok"`
      pairs with a real `p_miss`; `quality == "insufficient"` always pairs
      with `p_miss = None` -- never a fabricated/placeholder number
      (CLAUDE.md's "LLM agents never compute numbers" rule extends here).
    - `typical_wait_seconds` answers "if you do miss it, how long do you
      wait for the next one" -- computed from the OUTGOING leg's headway
      distribution. Gated on a confidence interval on this MEAN (2026-10-15,
      replacing a flat `n >= MIN_N_OBSERVATIONS` floor -- see
      `risk_engine.WAIT_MEAN_CI_FRACTION_MAX`'s docstring, the direct analog
      of `p_miss`'s Wilson gate for a mean instead of a proportion), citable
      once the interval is within 20% of the mean itself, regardless of raw
      n. `None` when that bucket is insufficient, same never-fabricate
      discipline, independent of whether `quality` is "ok".

    There is no `should_offer_reroute` field (2026-10-15, user: delete that
    feature entirely) -- the agent always asks the rider whether they want
    a faster route calculated whenever either `p_miss` or
    `typical_wait_seconds` is citable, unconditionally, no separate gating
    computation. See `ConversationAgent.SYSTEM_PROMPT`'s "Wait time and
    rerouting" section for the actual instruction, and its "Calculating the
    faster route" section for what happens once the rider says yes (or
    reports missing the transfer and being unable to wait): a fresh, live
    `plan_route` call from their current location, picking whatever OTP
    says is fastest right now -- never estimated from this historical data,
    and not necessarily even a different route than the one they had.

    These two signals are meant to always be narrated together when both
    are available (e.g. "55% chance of missing it, but the next one comes
    in 4 minutes") -- a bare miss percentage without the wait-time context
    can read as more alarming than it should on a high-frequency route.
    """

    from_route: str  # OTP-visible short name, e.g. "F" or "Q70-SBS" -- for narration only
    to_route: str
    transfer_stop_name: str  # human-readable, e.g. "Roosevelt Island"
    p_miss: float | None
    n: float  # incoming leg's bucket n_observations -- the bucket p_miss is computed from; 0 if missing
    window_days: int
    quality: Literal["ok", "insufficient"]
    typical_wait_seconds: float | None = None
