import json
from datetime import date, datetime, timezone

import pytest

from db import get_connection
from scripts.aggregate_reliability_buckets import HIST_CONFIG, run_aggregate

SERVICE_DATE = date(2026, 8, 24)  # a real Monday -> "weekday"


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "risk.sqlite3"))
    yield connection
    connection.close()


def _insert_event(conn, **overrides):
    fields = dict(
        agency="subway",
        route_id="F",
        direction="N",
        stop_id="B06",
        vehicle_id="F_1",
        trip_id=None,
        observed_arrival_ts=datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc),
        scheduled_arrival_ts=None,
        delay_seconds=None,
        predicted_arrival_ts_at_T_minus_5=None,
        service_date=SERVICE_DATE,
        day_type="weekday",
        hour_bucket=8,
        derivation_quality="clean",
    )
    fields.update(overrides)
    conn.execute(
        """
        INSERT INTO arrival_events (
            agency, route_id, direction, stop_id, vehicle_id, trip_id,
            observed_arrival_ts, scheduled_arrival_ts, delay_seconds,
            predicted_arrival_ts_at_T_minus_5, service_date, day_type,
            hour_bucket, derivation_quality
        ) VALUES (
            :agency, :route_id, :direction, :stop_id, :vehicle_id, :trip_id,
            :observed_arrival_ts, :scheduled_arrival_ts, :delay_seconds,
            :predicted_arrival_ts_at_T_minus_5, :service_date, :day_type,
            :hour_bucket, :derivation_quality
        )
        """,
        {
            **fields,
            "observed_arrival_ts": fields["observed_arrival_ts"].isoformat(),
            "scheduled_arrival_ts": (
                fields["scheduled_arrival_ts"].isoformat() if fields["scheduled_arrival_ts"] else None
            ),
            "predicted_arrival_ts_at_T_minus_5": (
                fields["predicted_arrival_ts_at_T_minus_5"].isoformat()
                if fields["predicted_arrival_ts_at_T_minus_5"]
                else None
            ),
            "service_date": fields["service_date"].isoformat(),
        },
    )
    conn.commit()


def _get_bucket(conn, agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type):
    row = conn.execute(
        """
        SELECT * FROM reliability_buckets
        WHERE agency = ? AND route_id = ? AND stop_id = ? AND direction = ?
          AND day_type = ? AND hour_bucket = ? AND stat_type = ?
        """,
        (agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["histogram"] = json.loads(d["histogram"])
    return d


def test_fresh_bucket_created_with_no_decay(conn):
    _insert_event(
        conn,
        delay_seconds=45,
        hour_bucket=8,
    )
    run_aggregate(conn)

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    assert bucket is not None
    assert bucket["n_observations"] == 1
    assert bucket["n_ambiguous"] == 0
    assert bucket["window_start"] == SERVICE_DATE.isoformat()
    # 45s lands in bin (45 - (-600)) // 30 = 21
    expected_idx = (45 - HIST_CONFIG["delay"]["min_s"]) // HIST_CONFIG["delay"]["bin_width_s"]
    assert bucket["histogram"]["counts"][expected_idx] == 1.0
    assert sum(bucket["histogram"]["counts"]) == 1.0


def test_second_day_applies_decay_exactly(conn):
    # Day 1: one delay observation of 45s.
    _insert_event(conn, delay_seconds=45, service_date=SERVICE_DATE)
    run_aggregate(conn)

    bucket_before = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    assert bucket_before["n_observations"] == 1

    # Day 2: one delay observation of 75s, same bucket key.
    day2 = date(2026, 8, 25)
    _insert_event(conn, delay_seconds=75, service_date=day2)
    run_aggregate(conn)

    bucket_after = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    idx_45 = (45 - HIST_CONFIG["delay"]["min_s"]) // HIST_CONFIG["delay"]["bin_width_s"]
    idx_75 = (75 - HIST_CONFIG["delay"]["min_s"]) // HIST_CONFIG["delay"]["bin_width_s"]

    assert bucket_after["n_observations"] == pytest.approx(0.95 * 1 + 0.05 * 1)
    assert bucket_after["window_start"] == SERVICE_DATE.isoformat()  # unchanged anchor

    if idx_45 == idx_75:
        assert bucket_after["histogram"]["counts"][idx_45] == pytest.approx(0.95 * 1 + 0.05 * 1)
    else:
        assert bucket_after["histogram"]["counts"][idx_45] == pytest.approx(0.95 * 1 + 0.05 * 0)
        assert bucket_after["histogram"]["counts"][idx_75] == pytest.approx(0.95 * 0 + 0.05 * 1)


def test_headway_assigned_to_later_events_hour_bucket(conn):
    # Two events, same (route, stop, direction), 90s apart. The later
    # event is in hour_bucket 9 (crossing an hour boundary on purpose) --
    # the headway value must land in hour_bucket 9, not 8.
    _insert_event(
        conn,
        observed_arrival_ts=datetime(2026, 8, 24, 8, 59, 30, tzinfo=timezone.utc),
        hour_bucket=8,
    )
    _insert_event(
        conn,
        observed_arrival_ts=datetime(2026, 8, 24, 9, 1, 0, tzinfo=timezone.utc),
        hour_bucket=9,
        vehicle_id="F_2",
    )
    run_aggregate(conn)

    bucket_9 = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 9, "headway")
    bucket_8 = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "headway")

    assert bucket_8 is None
    assert bucket_9 is not None
    assert bucket_9["n_observations"] == 1
    # 90s headway -> bin (90 - 0) // 30 = 3
    assert bucket_9["histogram"]["counts"][3] == 1.0


def test_bus_day_produces_zero_delay_buckets_but_other_stats_populate(conn):
    _insert_event(
        conn,
        agency="bus",
        route_id="Q70+",
        stop_id="504321",
        vehicle_id="MTABC_1",
        delay_seconds=None,
        predicted_arrival_ts_at_T_minus_5=datetime(2026, 8, 24, 7, 55, 0, tzinfo=timezone.utc),
        observed_arrival_ts=datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc),
    )
    _insert_event(
        conn,
        agency="bus",
        route_id="Q70+",
        stop_id="504321",
        vehicle_id="MTABC_2",
        delay_seconds=None,
        observed_arrival_ts=datetime(2026, 8, 24, 8, 10, 0, tzinfo=timezone.utc),
    )
    run_aggregate(conn)

    delay_bucket = _get_bucket(conn, "bus", "Q70+", "504321", "N", "weekday", 8, "delay")
    headway_bucket = _get_bucket(conn, "bus", "Q70+", "504321", "N", "weekday", 8, "headway")
    pred_bucket = _get_bucket(conn, "bus", "Q70+", "504321", "N", "weekday", 8, "prediction_error")

    assert delay_bucket is None
    assert headway_bucket is not None
    assert headway_bucket["n_observations"] == 1
    assert pred_bucket is not None
    assert pred_bucket["n_observations"] == 1

    total_delay_buckets = conn.execute(
        "SELECT COUNT(*) AS c FROM reliability_buckets WHERE agency = 'bus' AND stat_type = 'delay'"
    ).fetchone()["c"]
    assert total_delay_buckets == 0


def test_ambiguous_prediction_error_excluded_from_histogram_but_counted_in_n_ambiguous(conn):
    _insert_event(
        conn,
        predicted_arrival_ts_at_T_minus_5=datetime(2026, 8, 24, 7, 55, 0, tzinfo=timezone.utc),
        observed_arrival_ts=datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc),
        derivation_quality="ambiguous",
    )
    run_aggregate(conn)

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "prediction_error")
    assert bucket is not None
    assert bucket["n_observations"] == 0
    assert bucket["n_ambiguous"] == 1
    assert sum(bucket["histogram"]["counts"]) == 0.0


def test_rerunning_already_processed_day_is_a_noop(conn):
    _insert_event(conn, delay_seconds=45)
    run_aggregate(conn)

    bucket_first = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")

    # Re-run with no new data: (subway, SERVICE_DATE) is already in
    # processed_days, so find_unprocessed_days must skip it entirely --
    # the bucket must not be decayed a second time against itself.
    run_aggregate(conn)

    bucket_second = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    assert bucket_second["histogram"]["counts"] == bucket_first["histogram"]["counts"]
    assert bucket_second["n_observations"] == bucket_first["n_observations"]
    assert bucket_second["last_updated"] == bucket_first["last_updated"]


def test_headway_at_or_above_40_minutes_clips_to_last_bin(conn):
    _insert_event(
        conn,
        observed_arrival_ts=datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc),
        vehicle_id="F_1",
    )
    _insert_event(
        conn,
        observed_arrival_ts=datetime(2026, 8, 24, 8, 45, 0, tzinfo=timezone.utc),  # 45 min later
        vehicle_id="F_2",
        hour_bucket=8,
    )
    run_aggregate(conn)

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "headway")
    n_bins = HIST_CONFIG["headway"]["n_bins"]
    assert bucket["histogram"]["counts"][n_bins - 1] == 1.0
    assert sum(bucket["histogram"]["counts"]) == 1.0


def test_delay_below_negative_10_minutes_clips_to_first_bin(conn):
    _insert_event(conn, delay_seconds=-700)  # more than 10 min early
    run_aggregate(conn)

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    assert bucket["histogram"]["counts"][0] == 1.0
    assert sum(bucket["histogram"]["counts"]) == 1.0


def test_delay_above_40_minutes_clips_to_last_bin(conn):
    _insert_event(conn, delay_seconds=3000)  # 50 min late
    run_aggregate(conn)

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    n_bins = HIST_CONFIG["delay"]["n_bins"]
    assert bucket["histogram"]["counts"][n_bins - 1] == 1.0


def test_process_day_marks_processed_days(conn):
    _insert_event(conn)
    run_aggregate(conn)

    row = conn.execute(
        "SELECT * FROM processed_days WHERE agency = 'subway' AND service_date = ?",
        (SERVICE_DATE.isoformat(),),
    ).fetchone()
    assert row is not None
    assert row["processed_at"] is not None


def test_backlog_of_multiple_unprocessed_days_all_get_folded(conn):
    day1 = date(2026, 8, 24)
    day2 = date(2026, 8, 25)
    _insert_event(conn, delay_seconds=10, service_date=day1)
    _insert_event(conn, delay_seconds=20, service_date=day2, vehicle_id="F_2")

    run_aggregate(conn)

    processed = {
        row["service_date"]
        for row in conn.execute(
            "SELECT service_date FROM processed_days WHERE agency = 'subway'"
        ).fetchall()
    }
    assert processed == {day1.isoformat(), day2.isoformat()}

    bucket = _get_bucket(conn, "subway", "F", "B06", "N", "weekday", 8, "delay")
    # day1 creates the bucket fresh, day2 decays it in the same run.
    assert bucket["n_observations"] == pytest.approx(0.95 * 1 + 0.05 * 1)
