"""SQLite connection + schema helper for the Phase 2 risk engine.

Raw sqlite3, no ORM (Plan Decision 1: "DB: raw sqlite3, no ORM. Pydantic
models at the read/write boundary" -- see `app/models/risk.py`).

There is no migration framework in this project. Every consumer --
collector/import scripts (Tasks 2-4), the nightly aggregator (Task 5),
the risk-engine query layer (Task 6), and tests -- calls get_connection()
and gets the current schema for free via `CREATE TABLE IF NOT EXISTS`.

Scope note: this module is schema + connection only. It intentionally
does not include insert/upsert helpers -- those belong to the tasks that
actually write data (Task 4's arrival-event derivation, Task 5's nightly
upsert-by-key fold-in), since the shape of that logic depends on
decisions (dedup strategy, decay formula) this task isn't making.
"""
import sqlite3
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
    conn.executescript(SCHEMA)
    return conn
