"""SQLite connection + schema helper for the Phase 2 risk engine and
Phase 3 monitoring.

Raw sqlite3, no ORM (Plan Decision 1: "DB: raw sqlite3, no ORM. Pydantic
models at the read/write boundary" -- see `app/models/risk.py`).

There is no migration framework in this project. Every consumer --
collector/import scripts (Tasks 2-4), the nightly aggregator (Task 5),
the risk-engine query layer (Task 6), the Trip Monitor and /chat
notification surfacing (Phase 3), and tests -- calls get_connection()
and gets the current schema for free via `CREATE TABLE IF NOT EXISTS`.

Scope note: this module is schema + connection + the two atomic-claim
primitives below. It intentionally does not include general
insert/upsert helpers -- those belong to the tasks that actually write
data (Task 4's arrival-event derivation, Task 5's nightly upsert-by-key
fold-in, later Phase 3 tasks' create_monitored_trip/cancel_monitored_trip
tools), since the shape of that logic depends on decisions (dedup
strategy, decay formula, tool schemas) this module isn't making. The two
claim functions are an exception to "no helpers here": they exist in
this module specifically because they must be single atomic SQL
statements (see their docstrings) -- that's a connection/schema-layer
correctness concern, not business logic, so it belongs alongside
get_connection() rather than being reinvented ad hoc by each caller.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS arrival_events (
    id INTEGER PRIMARY KEY,
    agency TEXT,
    route_id TEXT,
    direction TEXT,
    stop_id TEXT,
    vehicle_id TEXT,
    trip_id TEXT NULL,
    observed_arrival_ts TIMESTAMP,
    scheduled_arrival_ts TIMESTAMP NULL,
    delay_seconds INT NULL,
    predicted_arrival_ts_at_T_minus_5 TIMESTAMP NULL,
    service_date DATE,
    day_type TEXT,
    hour_bucket INT,
    derivation_quality TEXT
);

-- Ingestion/import access pattern for Tasks 3 and 4 (not unique --
-- actual dedup on (vehicle_id, stop_id, service_date) is Task 4's job).
CREATE INDEX IF NOT EXISTS idx_arrival_events_ingest
    ON arrival_events (agency, route_id, stop_id, service_date);

CREATE TABLE IF NOT EXISTS reliability_buckets (
    id INTEGER PRIMARY KEY,
    agency TEXT,
    route_id TEXT,
    stop_id TEXT,
    direction TEXT,
    day_type TEXT,
    hour_bucket INT,
    stat_type TEXT,
    histogram JSON,
    n_observations INT,
    n_ambiguous INT,
    window_start DATE,
    last_updated TIMESTAMP
);

-- One row per bucket: Task 6 reads by this exact 7-tuple, Task 5's
-- nightly job upserts by this exact 7-tuple (decay formula, in place).
CREATE UNIQUE INDEX IF NOT EXISTS idx_reliability_buckets_key
    ON reliability_buckets (
        agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type
    );

-- Task 5 bookkeeping: which (agency, service_date) days have already been
-- folded into reliability_buckets. The decay formula is not idempotent --
-- folding the same day twice would double-apply it -- so a day is only
-- inserted here after its whole fold (all bucket upserts) commits.
CREATE TABLE IF NOT EXISTS processed_days (
    agency TEXT NOT NULL,
    service_date DATE NOT NULL,
    processed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (agency, service_date)
);

-- Phase 3 (Task 1): one row per user-facing monitored trip.
-- itinerary_snapshot is Itinerary.model_dump_json() stored as TEXT (sqlite
-- has no real JSON type -- see this task's brief for why TEXT is the
-- deliberate choice here rather than the "JSON" affinity label used on
-- reliability_buckets.histogram above).
CREATE TABLE IF NOT EXISTS monitored_trips (
    id INTEGER PRIMARY KEY,
    anonymous_id TEXT NOT NULL,
    itinerary_snapshot TEXT NOT NULL,
    deadline_ts TIMESTAMP NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    ttl_expires_at TIMESTAMP NOT NULL,
    last_checked_at TIMESTAMP NULL,
    pending_notification TEXT NULL
);

-- Every /chat call looks up pending notifications by anonymous_id.
CREATE INDEX IF NOT EXISTS idx_monitored_trips_anonymous_id
    ON monitored_trips (anonymous_id);

-- The Trip Monitor's poll-claim query and TTL sweep both filter on
-- (status, ttl_expires_at).
CREATE INDEX IF NOT EXISTS idx_monitored_trips_poll
    ON monitored_trips (status, ttl_expires_at);
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open the risk-engine sqlite database, creating it if needed.

    Creates parent directories if missing and ensures both tables/indexes
    exist (idempotent -- safe to call on every process start). Row
    factory is sqlite3.Row so callers can read columns by name, matching
    the read/write-boundary decision (Pydantic models parse dict-like
    rows, not positional tuples).

    db_path defaults to the configured `settings.db_path`; pass an
    explicit path (e.g. a tmp_path in tests) to override.
    """
    path = Path(db_path if db_path is not None else settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Phase 3: the 60s Trip Monitor poll loop and every /chat request now
    # read/write this same file concurrently (previously all access was
    # single-process batch jobs). WAL lets readers and writers proceed
    # without blocking each other; busy_timeout makes a concurrent writer
    # retry for up to 5s under contention instead of failing instantly
    # with "database is locked". Applied to every connection this function
    # opens -- including Phase 2's existing callers -- which is intentional
    # (design doc's Concurrency section): WAL only changes behavior under
    # concurrent access, so it's free insurance for the nightly aggregator
    # too, not just new Phase 3 code.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def claim_pending_notifications(conn: sqlite3.Connection, anonymous_id: str) -> list[dict]:
    """Atomically clear and return any pending notifications for a user.

    One UPDATE...RETURNING statement, not a SELECT followed by a separate
    UPDATE -- a select-then-clear has a real race window where two
    concurrent callers (two browser tabs, a double-send) could both read
    the same pending text before either clears it, showing it twice.
    RETURNING closes that window by construction: the row is only
    returned to whichever caller's UPDATE actually matched and cleared it.

    Deviates from the brief's literal `RETURNING id, pending_notification`
    on purpose, verified live against sqlite 3.49.1: RETURNING reflects
    the row's POST-update state, not its pre-update state (same as
    Postgres). A plain `RETURNING pending_notification` after
    `SET pending_notification = NULL` always returns NULL -- it can never
    surface the text that was just cleared, which is the entire point of
    this function. The fix is a `WITH ... AS MATERIALIZED` CTE that reads
    the old value before the UPDATE runs, forced to materialize (not
    re-inlined/re-evaluated after the UPDATE mutates the table -- checked
    directly: without MATERIALIZED this silently degrades back to
    returning NULL), then RETURNING joins back to that captured pre-image
    by id. Still one atomic SQL statement / one execute() call -- no
    SELECT-then-UPDATE round trip from Python.

    Returns a plain dict per claimed row (via dict(sqlite3.Row)), not a
    MonitoredTrip -- parsing itinerary_snapshot back into an Itinerary is
    the caller's job, not this module's (see module docstring).
    """
    cursor = conn.execute(
        """
        WITH claimed AS MATERIALIZED (
            SELECT id, pending_notification AS old_notification
            FROM monitored_trips
            WHERE anonymous_id = ? AND pending_notification IS NOT NULL
        )
        UPDATE monitored_trips SET pending_notification = NULL
        WHERE id IN (SELECT id FROM claimed)
        RETURNING
            id,
            (SELECT old_notification FROM claimed WHERE claimed.id = monitored_trips.id)
                AS pending_notification
        """,
        (anonymous_id,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.commit()
    return rows


def claim_active_trips_for_polling(conn: sqlite3.Connection, staleness_seconds: int) -> list[dict]:
    """Atomically claim active trips due for a poll check.

    One UPDATE...RETURNING statement, same atomicity reasoning as
    claim_pending_notifications above. Today there is exactly one backend
    process, so this is a no-op protection -- but it means the same
    polling code stays correct if the backend later runs with multiple
    uvicorn workers or Railway replicas: each trip is only claimed by
    whichever racer's UPDATE lands first, never claimed twice in the same
    window.

    Returns a plain dict per claimed row, not a MonitoredTrip (see
    module docstring and claim_pending_notifications above).
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=staleness_seconds)
    cursor = conn.execute(
        """
        UPDATE monitored_trips SET last_checked_at = ?
        WHERE status = 'active'
          AND (last_checked_at IS NULL OR last_checked_at < ?)
        RETURNING *
        """,
        (now.isoformat(), stale_before.isoformat()),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.commit()
    return rows
