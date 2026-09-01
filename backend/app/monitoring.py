"""backend/app/monitoring.py

`create_monitored_trip` / `cancel_monitored_trip` / `list_active_trips` --
Phase 3 Task 5's tool-backing functions for the Conversation Agent's
`create_monitored_trip` and `cancel_monitored_trip` tools (see
`.superpowers/sdd/task-5-brief.md`). Pure functions, no LLM calls -- same
discipline as `risk_engine.get_risk` / `deadline.compute_deadline_threshold`.

Lives here rather than in `db.py`, per `db.py`'s own module docstring,
which explicitly scopes itself to schema + connection + the two atomic-
claim primitives and names this exact task's create_monitored_trip/
cancel_monitored_trip as belonging elsewhere. Lives here rather than in
`app/agents/tools.py`, which holds pure tool-schema dicts and no logic.
This module follows the same standalone-`app/`-module precedent as
`risk_engine.py`/`deadline.py`/`alerts.py`.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary
from db import get_connection


def create_monitored_trip(
    itinerary: Itinerary,
    anonymous_id: str,
    deadline_ts: int | None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert a new active `monitored_trips` row for `itinerary` and
    return its id.

    `deadline_ts` is an epoch-ms int (matching
    `compute_deadline_threshold`'s convention), converted to a `datetime`
    here -- once, at this write boundary -- since the stored
    `MonitoredTrip.deadline_ts` field is typed `datetime`. `None` stores
    `NULL`.

    `ttl_expires_at` is the itinerary's own scheduled arrival (its last
    leg's `end_time_ms`, regardless of mode -- a trailing WALK leg's
    scheduled arrival is still real) plus 30 minutes (spec Sec 6).

    Does NOT call `compute_deadline_threshold` itself -- that happens one
    layer up, in the Conversation Agent's dispatch code, so this
    function's return type stays a plain `int` per the plan's literal
    signature (no richer return shape needed).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        deadline_dt = (
            datetime.fromtimestamp(deadline_ts / 1000, tz=timezone.utc)
            if deadline_ts is not None
            else None
        )
        created_at = datetime.now(timezone.utc)
        ttl_expires_at = datetime.fromtimestamp(
            itinerary.legs[-1].end_time_ms / 1000, tz=timezone.utc
        ) + timedelta(minutes=30)

        cursor = conn.execute(
            """
            INSERT INTO monitored_trips (
                anonymous_id, itinerary_snapshot, deadline_ts, status,
                created_at, ttl_expires_at, last_checked_at, pending_notification
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                anonymous_id,
                itinerary.model_dump_json(),
                deadline_dt.isoformat() if deadline_dt is not None else None,
                "active",
                created_at.isoformat(),
                ttl_expires_at.isoformat(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        if owns_conn:
            conn.close()


def cancel_monitored_trip(
    trip_id: int,
    anonymous_id: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Atomically cancel a trip -- one `UPDATE`, not a `SELECT`-then-check
    (matches Task 1's atomicity discipline, and avoids ever revealing to
    a caller whether `trip_id` exists at all for a *different*
    `anonymous_id`, which a two-step check-then-act could leak).

    Returns `True` if exactly one row matched and was cancelled, else
    `False` -- covers "trip doesn't exist," "belongs to a different
    anonymous_id," and "already cancelled/completed/expired" identically.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE monitored_trips SET status = 'cancelled'
            WHERE id = ? AND anonymous_id = ? AND status = 'active'
            """,
            (trip_id, anonymous_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        if owns_conn:
            conn.close()


def list_active_trips(conn: sqlite3.Connection, anonymous_id: str) -> list[MonitoredTrip]:
    """Every `status='active'` trip for `anonymous_id`, parsed into
    `MonitoredTrip` (`itinerary_snapshot` round-tripped via
    `Itinerary.model_validate_json`, matching Task 1's established
    read/write-boundary pattern).

    Required for the Conversation Agent's `cancel_monitored_trip`
    dispatch to disambiguate when the LLM doesn't have a specific
    `trip_id` yet. This crosses into `MonitoredTrip` territory
    deliberately -- agent-facing disambiguation, not a schema-layer
    primitive -- so it belongs here rather than alongside `db.py`'s two
    atomic claim functions (see that module's own scope note).
    """
    rows = conn.execute(
        "SELECT * FROM monitored_trips WHERE anonymous_id = ? AND status = 'active'",
        (anonymous_id,),
    ).fetchall()
    trips = []
    for row in rows:
        row_dict = dict(row)
        row_dict["itinerary_snapshot"] = Itinerary.model_validate_json(row_dict["itinerary_snapshot"])
        trips.append(MonitoredTrip.model_validate(row_dict))
    return trips
