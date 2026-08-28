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
    n_observations: int
    n_ambiguous: int
    window_start: date
    last_updated: datetime
