"""Pydantic model for the Phase 3 `monitored_trips` sqlite table (design
doc `docs/superpowers/specs/2026-08-31-phase-3-monitoring-design.md`).

Mirrors `monitored_trips` in `backend/db.py` field-for-field. Per Plan
Decision 1 ("raw sqlite3, no ORM; Pydantic models at the read/write
boundary" -- see `app/models/risk.py`), this is not an ORM layer -- it
exists so later Phase 3 tasks (Trip Monitor, Re-plan Agent,
create_monitored_trip/cancel_monitored_trip tools) can validate rows in
and out of sqlite3.Row dicts instead of passing bare dicts around.

Note the read/write-boundary split: `itinerary_snapshot` here is the
*parsed* `Itinerary`, not the raw JSON TEXT stored in the column. Code
that talks to the column directly (this task's two claim functions in
db.py) works with plain dicts and leaves itinerary_snapshot as a JSON
string -- it's the caller's job to decide when to parse it via
`Itinerary.model_validate_json(...)` and build a MonitoredTrip.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.transit import Itinerary

MonitoredTripStatus = Literal["active", "completed", "cancelled", "expired"]


class MonitoredTrip(BaseModel):
    """A single user's monitored trip, from creation through terminal status."""

    id: int | None = None
    anonymous_id: str
    itinerary_snapshot: Itinerary
    deadline_ts: datetime | None = None
    status: MonitoredTripStatus
    created_at: datetime
    ttl_expires_at: datetime
    last_checked_at: datetime | None = None
    pending_notification: str | None = None
