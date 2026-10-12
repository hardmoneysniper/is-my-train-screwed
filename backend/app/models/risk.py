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
      distribution vs. the planned transfer buffer. Gated on the incoming
      bucket's own n_observations alone. `quality == "ok"` pairs with a
      real `p_miss`; `quality == "insufficient"` always pairs with
      `p_miss = None` -- never a fabricated/placeholder number (CLAUDE.md's
      "LLM agents never compute numbers" rule extends to this function).
    - `typical_wait_seconds` answers "if you do miss it, how long do you
      wait for the next one" -- computed from the OUTGOING leg's headway
      distribution. Gated on the outgoing bucket's own n_observations,
      independent of whether `quality` is "ok". `None` when that bucket is
      insufficient, same never-fabricate discipline.
    - `should_offer_reroute` has no magnitude threshold (2026-10-12, user:
      "avoid the gate at all") -- it's simply whether `typical_wait_seconds`
      is citable at all (`True` whenever it's not `None`). The agent always
      asks the rider if they want a faster route calculated whenever a wait
      time is citable, no matter how short or long; it's the rider's call,
      not a hardcoded cutoff.

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
    should_offer_reroute: bool = False
