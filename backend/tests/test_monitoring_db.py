import threading
from datetime import datetime, timedelta, timezone

from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary, Leg
from db import claim_active_trips_for_polling, claim_pending_notifications, get_connection


def _sample_itinerary() -> Itinerary:
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
                end_time_ms=1_700_001_800_000,
            )
        ],
    )


def _insert_trip(conn, **overrides):
    fields = dict(
        anonymous_id="anon-1",
        itinerary_snapshot=_sample_itinerary().model_dump_json(),
        deadline_ts=None,
        status="active",
        created_at=datetime(2026, 8, 28, 8, 0, 0, tzinfo=timezone.utc).isoformat(),
        ttl_expires_at=datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc).isoformat(),
        last_checked_at=None,
        pending_notification=None,
    )
    fields.update(overrides)
    cursor = conn.execute(
        """
        INSERT INTO monitored_trips (
            anonymous_id, itinerary_snapshot, deadline_ts, status,
            created_at, ttl_expires_at, last_checked_at, pending_notification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields["anonymous_id"],
            fields["itinerary_snapshot"],
            fields["deadline_ts"],
            fields["status"],
            fields["created_at"],
            fields["ttl_expires_at"],
            fields["last_checked_at"],
            fields["pending_notification"],
        ),
    )
    conn.commit()
    return cursor.lastrowid


def test_monitored_trips_table_created(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "monitored_trips" in tables


def test_monitored_trip_round_trip(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    itinerary = _sample_itinerary()
    trip_id = _insert_trip(conn, itinerary_snapshot=itinerary.model_dump_json())

    row = conn.execute("SELECT * FROM monitored_trips WHERE id = ?", (trip_id,)).fetchone()
    conn.close()

    row_dict = dict(row)
    # itinerary_snapshot is stored as a JSON TEXT column -- round-trip it
    # through the exact call a later task (Re-plan Agent) will use.
    row_dict["itinerary_snapshot"] = Itinerary.model_validate_json(row_dict["itinerary_snapshot"])
    trip = MonitoredTrip.model_validate(row_dict)

    assert trip.id == trip_id
    assert trip.anonymous_id == "anon-1"
    assert trip.itinerary_snapshot == itinerary
    assert trip.status == "active"
    assert trip.deadline_ts is None
    assert trip.last_checked_at is None
    assert trip.pending_notification is None


def test_monitored_trip_round_trip_with_deadline_and_pending_notification(tmp_path):
    conn = get_connection(str(tmp_path / "risk.sqlite3"))
    deadline = datetime(2026, 8, 28, 10, 30, 0, tzinfo=timezone.utc)
    checked = datetime(2026, 8, 28, 8, 5, 0, tzinfo=timezone.utc)
    trip_id = _insert_trip(
        conn,
        deadline_ts=deadline.isoformat(),
        status="active",
        last_checked_at=checked.isoformat(),
        pending_notification="Your F train is running 8 minutes late.",
    )

    row = conn.execute("SELECT * FROM monitored_trips WHERE id = ?", (trip_id,)).fetchone()
    conn.close()

    row_dict = dict(row)
    row_dict["itinerary_snapshot"] = Itinerary.model_validate_json(row_dict["itinerary_snapshot"])
    trip = MonitoredTrip.model_validate(row_dict)

    assert trip.deadline_ts == deadline
    assert trip.last_checked_at == checked
    assert trip.pending_notification == "Your F train is running 8 minutes late."


def test_claim_pending_notifications_returns_empty_list_when_none_pending(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    _insert_trip(conn, anonymous_id="anon-1", pending_notification=None)
    result = claim_pending_notifications(conn, "anon-1")
    conn.close()
    assert result == []


def test_claim_pending_notifications_returns_empty_list_for_unknown_anonymous_id(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    result = claim_pending_notifications(conn, "nobody-has-this-id")
    conn.close()
    assert result == []


def test_claim_pending_notifications_clears_and_returns_pending_row(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    trip_id = _insert_trip(
        conn, anonymous_id="anon-1", pending_notification="Your F train is delayed."
    )

    result = claim_pending_notifications(conn, "anon-1")
    assert result == [{"id": trip_id, "pending_notification": "Your F train is delayed."}]

    row = conn.execute(
        "SELECT pending_notification FROM monitored_trips WHERE id = ?", (trip_id,)
    ).fetchone()
    conn.close()
    assert row["pending_notification"] is None


def test_claim_pending_notifications_maps_distinct_text_per_row(tmp_path):
    # A user can have more than one monitored trip pending at once (e.g.
    # two separate legs of a day monitored independently). The
    # MATERIALIZED-CTE pre-image capture is correlated by id -- this
    # guards against a regression that accidentally returns the same
    # (e.g. first-matched) old value for every claimed row instead of
    # each row's own.
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    trip_a = _insert_trip(conn, anonymous_id="anon-1", pending_notification="trip A delayed")
    trip_b = _insert_trip(conn, anonymous_id="anon-1", pending_notification="trip B rerouted")
    _insert_trip(conn, anonymous_id="anon-2", pending_notification="not yours")

    result = claim_pending_notifications(conn, "anon-1")
    conn.close()

    by_id = {row["id"]: row["pending_notification"] for row in result}
    assert by_id == {trip_a: "trip A delayed", trip_b: "trip B rerouted"}


def test_claim_pending_notifications_only_claims_matching_anonymous_id(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    _insert_trip(conn, anonymous_id="anon-other", pending_notification="not yours")

    result = claim_pending_notifications(conn, "anon-1")
    conn.close()
    assert result == []


def test_claim_pending_notifications_is_atomic_under_real_concurrent_racers(tmp_path):
    """Two real OS threads, each with its own sqlite3.Connection (both via
    get_connection() against the same file), race to claim the same
    pending notification.

    Why this isn't the tautological version: a naive "call claim twice
    sequentially in the same thread" test would trivially pass even for
    a broken SELECT-then-UPDATE implementation, because by the time the
    second call runs, the first call has already finished start-to-finish
    (including its UPDATE) -- there's no way for the second call to
    observe the pre-clear state. That proves nothing about atomicity.

    This test instead uses a threading.Barrier to force both threads to
    issue their claim as close to simultaneously as possible, repeated
    across many independent trials (each against its own freshly
    inserted row/anonymous_id) to maximize the chance of genuinely
    overlapping execution. A SELECT-then-UPDATE implementation has a real
    window in WAL mode for both threads' SELECTs to read the same
    non-NULL value before either UPDATE commits -- which would surface
    here as *both* racers reporting a non-empty result for the same
    trial (double delivery). The correct single UPDATE...RETURNING
    statement cannot do that: sqlite serializes writers, so whichever
    UPDATE commits second no longer matches the WHERE clause (the value
    is already NULL) and returns zero rows.
    """
    db_path = str(tmp_path / "risk.sqlite3")
    n_trials = 40
    outcomes: list[tuple[bool, bool]] = []

    for i in range(n_trials):
        anon_id = f"anon-race-{i}"
        setup_conn = get_connection(db_path)
        trip_id = _insert_trip(
            setup_conn, anonymous_id=anon_id, pending_notification=f"notification-{i}"
        )
        setup_conn.close()

        barrier = threading.Barrier(2)
        won = {}

        def racer(slot):
            conn = get_connection(db_path)
            try:
                barrier.wait(timeout=5)
                claimed = claim_pending_notifications(conn, anon_id)
                won[slot] = any(r["id"] == trip_id for r in claimed)
            finally:
                conn.close()

        t1 = threading.Thread(target=racer, args=(0,))
        t2 = threading.Thread(target=racer, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes.append((won[0], won[1]))

    for i, (a, b) in enumerate(outcomes):
        assert (a, b) in [(True, False), (False, True)], (
            f"trial {i}: expected exactly one racer to claim the "
            f"notification exactly once, got racer0={a} racer1={b} "
            "(both True = double delivery, both False = lost notification)"
        )


def test_claim_active_trips_for_polling_excludes_recently_checked_trip(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    _insert_trip(conn, status="active", last_checked_at=recent.isoformat())

    result = claim_active_trips_for_polling(conn, staleness_seconds=60)
    conn.close()
    assert result == []


def test_claim_active_trips_for_polling_includes_never_checked_trip(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    trip_id = _insert_trip(conn, status="active", last_checked_at=None)

    result = claim_active_trips_for_polling(conn, staleness_seconds=60)
    conn.close()
    assert [row["id"] for row in result] == [trip_id]


def test_claim_active_trips_for_polling_includes_stale_trip(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    trip_id = _insert_trip(conn, status="active", last_checked_at=old.isoformat())

    result = claim_active_trips_for_polling(conn, staleness_seconds=60)
    conn.close()
    assert [row["id"] for row in result] == [trip_id]


def test_claim_active_trips_for_polling_excludes_non_active_status(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    _insert_trip(conn, status="completed", last_checked_at=None)
    _insert_trip(conn, status="cancelled", last_checked_at=None)
    _insert_trip(conn, status="expired", last_checked_at=None)

    result = claim_active_trips_for_polling(conn, staleness_seconds=60)
    conn.close()
    assert result == []


def test_claim_active_trips_for_polling_sets_last_checked_at(tmp_path):
    db_path = str(tmp_path / "risk.sqlite3")
    conn = get_connection(db_path)
    trip_id = _insert_trip(conn, status="active", last_checked_at=None)

    before = datetime.now(timezone.utc)
    result = claim_active_trips_for_polling(conn, staleness_seconds=60)
    after = datetime.now(timezone.utc)
    conn.close()

    assert len(result) == 1
    claimed_at = datetime.fromisoformat(result[0]["last_checked_at"])
    assert before <= claimed_at <= after
    assert result[0]["id"] == trip_id


def test_claim_active_trips_for_polling_is_atomic_under_real_concurrent_racers(tmp_path):
    """Same atomicity proof as claim_pending_notifications above, applied
    to the poll-claim query. Two real threads race to claim the same
    trip; exactly one must win. This is the property the design doc
    calls out explicitly: today there's one backend process so this is a
    no-op protection, but it's what lets `--workers N` or multiple
    Railway replicas poll from the same table later without a rewrite --
    each trip claimed by exactly one racer, never checked twice in the
    same cycle (duplicate re-plans) or missed entirely.
    """
    db_path = str(tmp_path / "risk.sqlite3")
    n_trials = 40
    outcomes: list[tuple[bool, bool]] = []

    for i in range(n_trials):
        setup_conn = get_connection(db_path)
        trip_id = _insert_trip(
            setup_conn,
            anonymous_id=f"anon-poll-race-{i}",
            status="active",
            last_checked_at=None,
        )
        setup_conn.close()

        barrier = threading.Barrier(2)
        won = {}

        def racer(slot):
            conn = get_connection(db_path)
            try:
                barrier.wait(timeout=5)
                # Large staleness window: previously claimed trips (from
                # earlier trials) now carry a fresh last_checked_at and
                # must stay excluded so each trial only races over its
                # own newly-inserted, never-checked row.
                claimed = claim_active_trips_for_polling(conn, staleness_seconds=3600)
                won[slot] = any(row["id"] == trip_id for row in claimed)
            finally:
                conn.close()

        t1 = threading.Thread(target=racer, args=(0,))
        t2 = threading.Thread(target=racer, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes.append((won[0], won[1]))

    for i, (a, b) in enumerate(outcomes):
        assert (a, b) in [(True, False), (False, True)], (
            f"trial {i}: expected exactly one racer to claim the trip "
            f"exactly once, got racer0={a} racer1={b} "
            "(both True = claimed twice in one cycle, both False = never claimed)"
        )
