# backend/tests/test_monitoring.py
from datetime import datetime, timedelta, timezone

import pytest

from app.models.transit import Itinerary, Leg
from app.monitoring import cancel_monitored_trip, create_monitored_trip, list_active_trips
from db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "monitoring.sqlite3"))
    yield connection
    connection.close()


def _itinerary(end_time_ms=1_700_001_800_000) -> Itinerary:
    return Itinerary(
        duration_seconds=1800,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="B06",
                from_stop_name="Roosevelt Island",
                to_stop_id="A25",
                to_stop_name="Lexington Ave/63 St",
                start_time_ms=1_700_000_000_000,
                end_time_ms=end_time_ms,
            )
        ],
    )


def _fetch_row(conn, trip_id):
    return conn.execute("SELECT * FROM monitored_trips WHERE id = ?", (trip_id,)).fetchone()


# --- create_monitored_trip --------------------------------------------------


def test_create_monitored_trip_inserts_row_with_computed_ttl(conn):
    itinerary = _itinerary(end_time_ms=1_700_001_800_000)
    trip_id = create_monitored_trip(itinerary, "anon-1", None, conn=conn)

    row = _fetch_row(conn, trip_id)
    assert row is not None
    expected_ttl = datetime.fromtimestamp(1_700_001_800_000 / 1000, tz=timezone.utc) + timedelta(minutes=30)
    assert datetime.fromisoformat(row["ttl_expires_at"]) == expected_ttl
    assert row["status"] == "active"
    assert row["last_checked_at"] is None


def test_create_monitored_trip_stores_itinerary_snapshot_and_anonymous_id(conn):
    itinerary = _itinerary()
    trip_id = create_monitored_trip(itinerary, "anon-1", None, conn=conn)

    row = _fetch_row(conn, trip_id)
    assert row["anonymous_id"] == "anon-1"
    assert Itinerary.model_validate_json(row["itinerary_snapshot"]) == itinerary
    assert row["pending_notification"] is None


def test_create_monitored_trip_with_no_deadline_stores_null(conn):
    trip_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    row = _fetch_row(conn, trip_id)
    assert row["deadline_ts"] is None


def test_create_monitored_trip_with_deadline_converts_epoch_ms_to_datetime(conn):
    deadline_ts = 1_700_010_000_000  # epoch-ms
    trip_id = create_monitored_trip(_itinerary(), "anon-1", deadline_ts, conn=conn)

    row = _fetch_row(conn, trip_id)
    expected = datetime.fromtimestamp(deadline_ts / 1000, tz=timezone.utc)
    assert datetime.fromisoformat(row["deadline_ts"]) == expected


def test_create_monitored_trip_returns_new_row_id(conn):
    trip_id_1 = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    trip_id_2 = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    assert trip_id_2 != trip_id_1
    assert _fetch_row(conn, trip_id_1) is not None
    assert _fetch_row(conn, trip_id_2) is not None


# --- cancel_monitored_trip ---------------------------------------------------


def test_cancel_monitored_trip_with_matching_owner_succeeds(conn):
    trip_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)

    result = cancel_monitored_trip(trip_id, "anon-1", conn=conn)

    assert result is True
    row = _fetch_row(conn, trip_id)
    assert row["status"] == "cancelled"


def test_cancel_monitored_trip_with_mismatched_owner_fails_and_does_not_mutate(conn):
    trip_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)

    result = cancel_monitored_trip(trip_id, "anon-2", conn=conn)

    assert result is False
    row = _fetch_row(conn, trip_id)
    assert row["status"] == "active"


def test_cancel_monitored_trip_twice_fails_the_second_time(conn):
    trip_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)

    first = cancel_monitored_trip(trip_id, "anon-1", conn=conn)
    second = cancel_monitored_trip(trip_id, "anon-1", conn=conn)

    assert first is True
    assert second is False


def test_cancel_monitored_trip_nonexistent_id_returns_false(conn):
    result = cancel_monitored_trip(999999, "anon-1", conn=conn)
    assert result is False


# --- list_active_trips --------------------------------------------------------


def test_list_active_trips_returns_only_active_rows_for_given_anonymous_id(conn):
    active_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    cancelled_id = create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    cancel_monitored_trip(cancelled_id, "anon-1", conn=conn)
    other_users_id = create_monitored_trip(_itinerary(), "anon-2", None, conn=conn)

    trips = list_active_trips(conn, "anon-1")

    assert [t.id for t in trips] == [active_id]
    assert other_users_id not in [t.id for t in trips]


def test_list_active_trips_parses_itinerary_snapshot_via_model_validate_json(conn):
    itinerary = _itinerary()
    trip_id = create_monitored_trip(itinerary, "anon-1", None, conn=conn)

    trips = list_active_trips(conn, "anon-1")

    assert len(trips) == 1
    assert trips[0].id == trip_id
    assert trips[0].itinerary_snapshot == itinerary
    assert trips[0].status == "active"


def test_list_active_trips_returns_empty_list_for_unknown_anonymous_id(conn):
    create_monitored_trip(_itinerary(), "anon-1", None, conn=conn)
    trips = list_active_trips(conn, "nobody-has-this-id")
    assert trips == []
