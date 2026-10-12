"""Tests for scripts/hourly_bus_sync.py's real logic: pull-then-delete
safety ordering, append-vs-overwrite by file type, and the real-bucket-
coverage check that decides when to stop collection (2026-10-12,
replacing the retired raw-record-target approach -- see
BUS_COLLECTION_TARGETS's module comment for why). railway ssh itself is
mocked -- not re-testing the CLI, testing that this script uses it
correctly and never deletes before a local write is confirmed.
Recurrence and overlap protection are handled by Windows Task Scheduler
(this script is one-shot, see its module docstring), not tested here."""
import gzip
import json
from unittest.mock import patch

from db import get_connection
from scripts.hourly_bus_sync import (
    check_and_maybe_stop_collection,
    coverage_complete,
    local_route_day_type_counts,
    real_bucket_coverage,
    sync_once,
)


def _insert_bucket(conn, route_id, day_type, n_observations, stop_id="12345", hour_bucket=8, stat_type="headway"):
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous, window_start,
            last_updated
        ) VALUES (
            'bus', ?, ?, 'N', ?, ?, ?, '{"bin_width_s":30,"min_s":0,"counts":[]}',
            ?, 0, '2026-08-14', '2026-10-12T00:00:00+00:00'
        )
        """,
        (route_id, stop_id, day_type, hour_bucket, stat_type, n_observations),
    )
    conn.commit()


def test_sync_once_appends_ndjson_but_overwrites_gz(tmp_path):
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "2026-09-22.ndjson").write_bytes(b"earlier-hour-content\n")

    deleted = []
    with patch("scripts.hourly_bus_sync.LOCAL_DIR", local_dir), \
         patch("scripts.hourly_bus_sync._list_remote_files", return_value=["2026-09-22.ndjson", "2026-09-21.ndjson.gz"]), \
         patch("scripts.hourly_bus_sync._pull_remote_file", side_effect=lambda name: {
             "2026-09-22.ndjson": b"this-hour-content\n",
             "2026-09-21.ndjson.gz": b"complete-gz-bytes",
         }[name]), \
         patch("scripts.hourly_bus_sync._delete_remote_file", side_effect=deleted.append):
        pulled = sync_once()

    assert (local_dir / "2026-09-22.ndjson").read_bytes() == b"earlier-hour-content\nthis-hour-content\n"
    assert (local_dir / "2026-09-21.ndjson.gz").read_bytes() == b"complete-gz-bytes"
    assert pulled == {"2026-09-22.ndjson": len(b"this-hour-content\n"), "2026-09-21.ndjson.gz": len(b"complete-gz-bytes")}
    assert set(deleted) == {"2026-09-22.ndjson", "2026-09-21.ndjson.gz"}


def test_sync_once_skips_empty_remote_files_without_deleting(tmp_path):
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    deleted = []
    with patch("scripts.hourly_bus_sync.LOCAL_DIR", local_dir), \
         patch("scripts.hourly_bus_sync._list_remote_files", return_value=["2026-09-22.ndjson"]), \
         patch("scripts.hourly_bus_sync._pull_remote_file", return_value=b""), \
         patch("scripts.hourly_bus_sync._delete_remote_file", side_effect=deleted.append):
        pulled = sync_once()

    assert pulled == {}
    assert deleted == []
    assert not (local_dir / "2026-09-22.ndjson").exists()


def test_local_route_day_type_counts_splits_weekday_and_weekend(tmp_path):
    # 2026-09-22 is a Tuesday (weekday); 2026-09-19 is a Saturday (weekend).
    (tmp_path / "2026-09-22.ndjson").write_text(
        "\n".join([
            json.dumps({"route_id": "M60+"}),
            json.dumps({"route_id": "Q70+"}),
            json.dumps({"route_id": "M60+"}),
            json.dumps({"route_id": "not-tracked"}),
        ])
    )
    with gzip.open(tmp_path / "2026-09-19.ndjson.gz", "wt") as f:
        f.write(json.dumps({"route_id": "Q102"}) + "\n")

    with patch("scripts.hourly_bus_sync.LOCAL_DIR", tmp_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q70+", "Q102"]):
        counts = local_route_day_type_counts()

    assert counts["M60+"] == {"weekday": 2, "weekend": 0}
    assert counts["Q70+"] == {"weekday": 1, "weekend": 0}
    assert counts["Q102"] == {"weekday": 0, "weekend": 1}


def test_real_bucket_coverage_measures_directly_against_reliability_buckets(tmp_path):
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        # M60+ weekday: 2 real buckets, 1 clears MIN_N_OBSERVATIONS (50).
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s1")
        _insert_bucket(conn, "M60+", "weekday", n_observations=10, stop_id="s2")
        # M60+ weekend: zero real buckets at all.
        # Q70+ weekday: only a prediction_error bucket (still counted --
        # both stat_types are real candidate buckets).
        _insert_bucket(conn, "Q70+", "weekday", n_observations=60, stop_id="s3", stat_type="prediction_error")
        # A zero-n_observations row must be excluded from the denominator
        # entirely (never-observed, not evidence either way).
        _insert_bucket(conn, "Q70+", "weekday", n_observations=0, stop_id="s4")
    finally:
        conn.close()

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q70+"]):
        coverage = real_bucket_coverage()

    assert coverage["M60+"]["weekday"] == (1, 2)
    assert coverage["M60+"]["weekend"] == (0, 0)
    assert coverage["Q70+"]["weekday"] == (1, 1)


def test_real_bucket_coverage_excludes_late_night_hours(tmp_path):
    # 2026-10-12 (user): risk_engine.py never cites a bus bucket before
    # bus_schedule_periods.ANCHOR_HOUR ("almost nobody rides a bus at
    # 2am") -- tracking coverage for it here would just be noise.
    from app import bus_schedule_periods
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        # A real, abundant 2am bucket -- must not count toward coverage.
        _insert_bucket(conn, "M60+", "weekday", n_observations=200, stop_id="s1", hour_bucket=2)
        # A real daytime bucket -- must count normally.
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s2", hour_bucket=8)
    finally:
        conn.close()

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]):
        coverage = real_bucket_coverage()

    assert coverage["M60+"]["weekday"] == (1, 1)
    assert bus_schedule_periods.ANCHOR_HOUR == 5  # sanity-check the boundary this test relies on


def test_coverage_complete_is_false_for_a_corridor_with_no_real_buckets():
    # Q3 has real-looking numbers for M60+ but zero evidence of its own
    # (total=0) -- must not count as "ready", zero real buckets is not
    # the same as 100% coverage.
    coverage = {
        "M60+": {"weekday": (95, 100), "weekend": (95, 100)},
        "Q3": {"weekday": (0, 0), "weekend": (0, 0)},
    }
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q3"]):
        assert coverage_complete(coverage) is False


def test_coverage_complete_is_false_when_weekend_still_below_fraction():
    coverage = {"M60+": {"weekday": (95, 100), "weekend": (50, 100)}}  # weekend only 50%
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]):
        assert coverage_complete(coverage) is False


def test_coverage_complete_is_true_once_every_tracked_corridor_clears_the_fraction():
    coverage = {
        "M60+": {"weekday": (91, 100), "weekend": (90, 100)},  # exactly 90%
        "Q70+": {"weekday": (18, 20), "weekend": (19, 20)},
    }
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q70+"]):
        assert coverage_complete(coverage) is True


def test_check_and_maybe_stop_collection_writes_marker_only_when_complete():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.categorize_bus_data"), \
         patch("scripts.hourly_bus_sync.real_bucket_coverage",
               return_value={"M60+": {"weekday": (10, 100), "weekend": (10, 100)}}), \
         patch("scripts.hourly_bus_sync._write_remote_marker", side_effect=lambda c: written.append(c)):
        stopped, coverage = check_and_maybe_stop_collection()
    assert stopped is False
    assert coverage == {"M60+": {"weekday": (10, 100), "weekend": (10, 100)}}
    assert written == []


def test_check_and_maybe_stop_collection_writes_marker_when_coverage_complete():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.categorize_bus_data"), \
         patch("scripts.hourly_bus_sync.real_bucket_coverage",
               return_value={"M60+": {"weekday": (95, 100), "weekend": (95, 100)}}), \
         patch("scripts.hourly_bus_sync._write_remote_marker", side_effect=lambda c: written.append(c)):
        stopped, coverage = check_and_maybe_stop_collection()
    assert stopped is True
    assert coverage == {"M60+": {"weekday": (95, 100), "weekend": (95, 100)}}
    assert len(written) == 1
