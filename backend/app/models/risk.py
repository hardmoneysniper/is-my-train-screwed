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
    # float, not int: Task 5's nightly decay (0.95*old + 0.05*today) applied
    # to n the same way as the histogram makes these genuinely fractional
    # after the first fold (e.g. 0.95), so a strict int field would raise
    # ValidationError on real decayed rows.
    n_observations: float
    n_ambiguous: float
    window_start: date
    last_updated: datetime


class TransferRisk(BaseModel):
    """Per-transfer result from `get_risk` (Task 6) -- one entry per
    transfer point in an itinerary, in itinerary order. `p_miss` and the
    rest of the Monte Carlo output are only meaningful when
    `quality == "ok"`; `quality == "insufficient"` always pairs with
    `p_miss = None` -- never a fabricated/placeholder number (CLAUDE.md's
    "LLM agents never compute numbers" rule extends to this function:
    nothing downstream may mistake a placeholder for a real probability).
    """

    from_route: str  # OTP-visible short name, e.g. "F" or "Q70-SBS" -- for narration only
    to_route: str
    transfer_stop_name: str  # human-readable, e.g. "Roosevelt Island"
    p_miss: float | None
    n: float  # min(n_observations) across the required buckets; 0 if a bucket was missing
    window_days: int
    quality: Literal["ok", "insufficient"]
