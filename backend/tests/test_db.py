import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.models.risk import ArrivalEvent, ReliabilityBucket
from db import get_connection


def _sample_event(**overrides):
    fields = dict(
        agency="MTA",
        route_id="F",
        direction="N",
        stop_id="B06",
        vehicle_id="F_123",
        trip_id=None,
        observed_arrival_ts=datetime(2026, 8, 28, 8, 15, 0, tzinfo=timezone.utc),
        scheduled_arrival_ts=None,
        delay_seconds=None,
        predicted_arrival_ts_at_T_minus_5=None,
        service_date=date(2026, 8, 28),
        day_type="weekday",
        hour_bucket=8,
        derivation_quality="clean",
    )
    fields.update(overrides)
    return ArrivalEvent(**fields)


def _insert_event(conn, event: ArrivalEvent):
    conn.execute(
        """
        INSERT INTO arrival_events (
            agency, route_id, direction, stop_id, vehicle_id, trip_id,
            observed_arrival_ts, scheduled_arrival_ts, delay_seconds,
            predicted_arrival_ts_at_T_minus_5, service_date, day_type,
            hour_bucket, derivation_quality
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.agency,
            event.route_id,
            event.direction,
            event.stop_id,
            event.vehicle_id,
            event.trip_id,
            event.observed_arrival_ts.isoformat(),
            event.scheduled_arrival_ts.isoformat() if event.scheduled_arrival_ts else None,
            event.delay_seconds,
            event.predicted_arrival_ts_at_T_minus_5.isoformat()
            if event.predicted_arrival_ts_at_T_minus_5
            else None,
            event.service_date.isoformat(),
            event.day_type,
            event.hour_bucket,
            event.derivation_quality,
        ),
    )


def _sample_bucket(**overrides):
    fields = dict(
        agency="MTA",
        route_id="F",
        stop_id="B06",
        direction="N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram={"0-30": 12, "30-60": 4},
        n_observations=16,
        n_ambiguous=1,
        window_start=date(2026, 8, 1),
        last_updated=datetime(2026, 8, 28, 3, 0, 0, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return ReliabilityBucket(**fields)


def _insert_bucket(conn, bucket: ReliabilityBucket):
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous,
            window_start, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bucket.agency,
            bucket.route_id,
            bucket.stop_id,
            bucket.direction,
            bucket.day_type,
            bucket.hour_bucket,
            bucket.stat_type,
            json.dumps(bucket.histogram),
            bucket.n_observations,
            bucket.n_ambiguous,
            bucket.window_start.isoformat(),
            bucket.last_updated.isoformat(),
        ),
    )


def test_get_connection_creates_both_tables(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"arrival_events", "reliability_buckets"} <= tables


def test_get_connection_creates_parent_directories(tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "risk.sqlite3"
    conn = get_connection(str(nested_path))
    conn.close()
    assert nested_path.exists()


def test_get_connection_is_idempotent(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    get_connection(db_path).close()
    # Second connect must not error on the already-existing schema.
    conn = get_connection(db_path)
    conn.close()


def test_get_connection_enables_wal_journal_mode(tmp_path):
    # Phase 3: WAL lets /chat reads and the Trip Monitor's writes proceed
    # without blocking each other. Assert the pragma actually took effect
    # -- don't just assume executing it "worked" -- since sqlite's default
    # (unset) journal_mode is "delete", a different value entirely.
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_get_connection_sets_busy_timeout(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout_ms == 5000


def test_arrival_event_round_trip(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    event = _sample_event()
    _insert_event(conn, event)
    conn.commit()

    row = conn.execute("SELECT * FROM arrival_events").fetchone()
    conn.close()

    round_tripped = ArrivalEvent.model_validate(dict(row))
    assert round_tripped.id is not None
    assert round_tripped.agency == "MTA"
    assert round_tripped.route_id == "F"
    assert round_tripped.direction == "N"
    assert round_tripped.stop_id == "B06"
    assert round_tripped.vehicle_id == "F_123"
    assert round_tripped.trip_id is None
    assert round_tripped.observed_arrival_ts == event.observed_arrival_ts
    assert round_tripped.scheduled_arrival_ts is None
    assert round_tripped.delay_seconds is None
    assert round_tripped.predicted_arrival_ts_at_T_minus_5 is None
    assert round_tripped.service_date == event.service_date
    assert round_tripped.day_type == "weekday"
    assert round_tripped.hour_bucket == 8
    assert round_tripped.derivation_quality == "clean"


def test_arrival_event_round_trip_with_all_nullable_fields_populated(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    event = _sample_event(
        trip_id="BSP26GEN-A087-Weekday-00_052250_6..N03R",
        scheduled_arrival_ts=datetime(2026, 8, 28, 8, 14, 30, tzinfo=timezone.utc),
        delay_seconds=30,
        predicted_arrival_ts_at_T_minus_5=datetime(2026, 8, 28, 8, 10, 0, tzinfo=timezone.utc),
        day_type="weekend",
        derivation_quality="ambiguous",
    )
    _insert_event(conn, event)
    conn.commit()

    row = conn.execute("SELECT * FROM arrival_events").fetchone()
    conn.close()

    round_tripped = ArrivalEvent.model_validate(dict(row))
    assert round_tripped.trip_id == event.trip_id
    assert round_tripped.scheduled_arrival_ts == event.scheduled_arrival_ts
    assert round_tripped.delay_seconds == 30
    assert round_tripped.predicted_arrival_ts_at_T_minus_5 == event.predicted_arrival_ts_at_T_minus_5
    assert round_tripped.day_type == "weekend"
    assert round_tripped.derivation_quality == "ambiguous"


def test_reliability_bucket_round_trip(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    bucket = _sample_bucket()
    _insert_bucket(conn, bucket)
    conn.commit()

    row = conn.execute("SELECT * FROM reliability_buckets").fetchone()
    conn.close()

    row_dict = dict(row)
    row_dict["histogram"] = json.loads(row_dict["histogram"])
    round_tripped = ReliabilityBucket.model_validate(row_dict)

    assert round_tripped.id is not None
    assert round_tripped.agency == "MTA"
    assert round_tripped.route_id == "F"
    assert round_tripped.stop_id == "B06"
    assert round_tripped.direction == "N"
    assert round_tripped.day_type == "weekday"
    assert round_tripped.hour_bucket == 8
    assert round_tripped.stat_type == "delay"
    assert round_tripped.histogram == {"0-30": 12, "30-60": 4}
    assert round_tripped.n_observations == 16
    assert round_tripped.n_ambiguous == 1
    assert round_tripped.window_start == bucket.window_start
    assert round_tripped.last_updated == bucket.last_updated


def test_reliability_buckets_unique_index_rejects_duplicate_key(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    _insert_bucket(conn, _sample_bucket())
    conn.commit()

    # Same (agency, route_id, stop_id, direction, day_type, hour_bucket,
    # stat_type) key -- only n_observations differs. The unique index
    # must reject this as an insert; Task 5 is responsible for building
    # the actual upsert-by-key logic on top of this constraint.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_bucket(conn, _sample_bucket(n_observations=99))
        conn.commit()

    conn.close()


def test_reliability_buckets_unique_index_allows_different_stat_type(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    _insert_bucket(conn, _sample_bucket(stat_type="delay"))
    _insert_bucket(conn, _sample_bucket(stat_type="headway"))
    conn.commit()

    count = conn.execute("SELECT COUNT(*) AS c FROM reliability_buckets").fetchone()["c"]
    conn.close()
    assert count == 2
