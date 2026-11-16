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
    REMOTE_DAY_TYPE_GATE,
    REMOTE_DONE_MARKER,
    _is_citable_row,
    check_and_maybe_stop_collection,
    coverage_complete,
    local_route_day_type_counts,
    needed_day_types,
    real_bucket_coverage,
    real_volume_coverage,
    sync_once,
)


def _citable_histogram(n_observations: int) -> str:
    """A single-bin histogram -- all `n_observations` weight at one
    midpoint, so variance (and therefore both the Wilson and mean-CI
    half-widths) is exactly 0, which always clears the real gate
    regardless of n (see risk_engine.py's citable_p_miss/
    citable_typical_wait_seconds): a perfectly concentrated distribution
    needs no minimum sample size to describe confidently. Deliberately
    the DEFAULT shape for these fixtures -- most of this file's existing
    cases only care about "is there a real bucket here", not about
    exercising the CI math itself (that's test_risk_engine.py's job),
    and real_bucket_coverage()/real_volume_coverage() (2026-11-11) no
    longer have a notion of "high n" being citable on its own, so a
    fixture that's citable regardless of n is the honest default."""
    return json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [n_observations]})


def _uncited_headway_histogram(n_observations: int) -> str:
    """A deliberately wide, bimodal headway histogram -- half the weight
    at one extreme, half at another ~3000s away -- for fixtures that
    must stay uncleared under the real mean-CI gate regardless of n.
    Used only where a test's whole point is "this bucket must NOT
    clear" (see _citable_histogram's docstring for the default/citable
    case every other fixture uses)."""
    half = n_observations // 2
    counts = [half] + [0] * 99 + [n_observations - half]
    return json.dumps({"bin_width_s": 30, "min_s": 0, "counts": counts})


def _insert_bucket(conn, route_id, day_type, n_observations, stop_id="12345", hour_bucket=8,
                    stat_type="headway", histogram=None):
    if histogram is None:
        histogram = _citable_histogram(n_observations)
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous, window_start,
            last_updated
        ) VALUES (
            'bus', ?, ?, 'N', ?, ?, ?, ?,
            ?, 0, '2026-08-14', '2026-10-12T00:00:00+00:00'
        )
        """,
        (route_id, stop_id, day_type, hour_bucket, stat_type, histogram, n_observations),
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


def test_is_citable_row_clears_a_low_n_but_confidently_concentrated_bucket():
    # The actual point of the 2026-11-11 fix: a small sample that's
    # tightly concentrated is citable even though it would NEVER have
    # cleared the old flat n>=50 proxy. n=12 is the same value
    # test_wilson_half_width_narrows_for_extreme_p_even_at_low_n already
    # pins at the lower level (12/12 clears P_MISS_CI_HALF_WIDTH_MAX);
    # a single-bin histogram's p_miss is always 1.0 (all-or-nothing
    # concentration), so this exercises the exact same real math through
    # the higher-level function this fix actually changed.
    assert _is_citable_row("headway", _citable_histogram(12)) is True
    assert _is_citable_row("prediction_error", _citable_histogram(12)) is True


def test_is_citable_row_depends_on_the_real_distribution_shape_not_just_raw_count():
    # The actual, correctly-behaved flip side: citability now depends on
    # the histogram's real shape, not a flat count -- at the SAME n (50,
    # the old flat threshold's own value), a concentrated bucket clears
    # the real gate while a genuinely wide/uncertain one at that
    # identical n does not. (A "does high n alone ever fail" framing
    # isn't the right test for a well-behaved CI gate: given enough real
    # samples confirming the SAME wide pattern, the mean's own estimate
    # legitimately tightens too -- see _uncited_headway_histogram(200)
    # in the volume-coverage test above, which correctly DOES clear.)
    assert _is_citable_row("headway", _citable_histogram(50)) is True
    assert _is_citable_row("headway", _uncited_headway_histogram(50)) is False


def test_is_citable_row_rejects_an_all_zero_histogram():
    empty = json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [0, 0, 0]})
    assert _is_citable_row("headway", empty) is False
    assert _is_citable_row("prediction_error", empty) is False


def test_real_bucket_coverage_measures_directly_against_reliability_buckets(tmp_path):
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        # M60+ weekday: 2 real buckets, 1 clears the real citability gate
        # (a concentrated/citable histogram by default -- see
        # _citable_histogram) and 1 that deliberately doesn't (a wide,
        # uncertain spread -- see _uncited_headway_histogram).
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s1")
        _insert_bucket(conn, "M60+", "weekday", n_observations=10, stop_id="s2",
                        histogram=_uncited_headway_histogram(10))
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


def _identity_period_mapping(route_id, day_type, hour):
    return (day_type, hour)


def test_real_bucket_coverage_excludes_the_2am_to_4am_window(tmp_path):
    # 2026-10-12 (user): risk_engine.py never cites a bus bucket in the
    # 2:00am-3:59am window ("almost nobody rides a bus at 2am") --
    # tracking coverage for it here would just be noise. 4am itself
    # (hour_bucket=4) must NOT be excluded -- "people start riding
    # buses/subways at 4am to catch early flights and go to work."
    # bus_schedule_periods.effective_day_type_and_hour is mocked as an
    # identity mapping here so the stored hour_bucket IS the literal hour
    # for this test -- see the next test for the real-world case where
    # it isn't (2026-10-25 fix).
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        _insert_bucket(conn, "M60+", "weekday", n_observations=200, stop_id="s1", hour_bucket=2)
        _insert_bucket(conn, "M60+", "weekday", n_observations=200, stop_id="s2", hour_bucket=3)
        # 4am and a normal daytime hour both count normally.
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s3", hour_bucket=4)
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s4", hour_bucket=8)
    finally:
        conn.close()

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.bus_schedule_periods.effective_day_type_and_hour", side_effect=_identity_period_mapping):
        coverage = real_bucket_coverage()

    assert coverage["M60+"]["weekday"] == (2, 2)


def test_real_bucket_coverage_excludes_the_real_mapped_late_night_period_not_the_literal_hour(tmp_path):
    # 2026-10-25 real bug fix: a stored bus hour_bucket is a PERIOD INDEX
    # (bus_schedule_periods' own cluster numbering), not a literal hour --
    # confirmed live for B15, whose real 2am-equivalent weekend period is
    # stored as hour_bucket=1, not 2. The old filter compared the stored
    # value directly against literal 2/3 and so never excluded it,
    # making B15 weekend coverage look stuck at a mysterious 0% forever
    # when the real story is just "all its real weekend buckets live in
    # one inherently low-traffic late-night period." This test pins the
    # fix: literal hours 2 and 3 both map to period hour_bucket=1 here,
    # and that's the value that must get excluded, not 2 or 3 themselves.
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        # This bucket IS the real late-night period (stored as 1) -- must
        # be excluded even though its literal stored value isn't 2 or 3.
        _insert_bucket(conn, "B15", "weekend", n_observations=17, stop_id="s1", hour_bucket=1)
        # A normal daytime period, stored as some other value, counts normally.
        _insert_bucket(conn, "B15", "weekend", n_observations=80, stop_id="s2", hour_bucket=8)
    finally:
        conn.close()

    def fake_mapping(route_id, day_type, hour):
        if hour in (2, 3):
            return (day_type, 1)
        return (day_type, hour)

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["B15"]), \
         patch("scripts.hourly_bus_sync.bus_schedule_periods.effective_day_type_and_hour", side_effect=fake_mapping):
        coverage = real_bucket_coverage()

    assert coverage["B15"]["weekend"] == (1, 1)


def test_real_volume_coverage_weights_by_observation_count_not_bucket_count(tmp_path):
    # 2026-10-20 (user): a second lens for fine-grained-grid routes whose
    # distinct-bucket-count metric looks stuck -- one high-traffic bucket
    # can dominate a route's real volume even while many low-traffic
    # buckets remain individually uncleared.
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        # One already-citable bucket carries almost all real traffic;
        # four more real buckets exist but are each thin AND genuinely
        # uncertain (a wide spread -- see _uncited_headway_histogram),
        # so they stay uncleared under the real gate too.
        _insert_bucket(conn, "Q102", "weekday", n_observations=400, stop_id="s1")
        _insert_bucket(conn, "Q102", "weekday", n_observations=5, stop_id="s2", histogram=_uncited_headway_histogram(5))
        _insert_bucket(conn, "Q102", "weekday", n_observations=5, stop_id="s3", histogram=_uncited_headway_histogram(5))
        _insert_bucket(conn, "Q102", "weekday", n_observations=5, stop_id="s4", histogram=_uncited_headway_histogram(5))
        _insert_bucket(conn, "Q102", "weekday", n_observations=5, stop_id="s5", histogram=_uncited_headway_histogram(5))
    finally:
        conn.close()

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["Q102"]):
        bucket_coverage = real_bucket_coverage()
        volume_coverage = real_volume_coverage()

    # Bucket-count view: only 1 of 5 real buckets clears the gate (20%).
    assert bucket_coverage["Q102"]["weekday"] == (1, 5)
    # Volume view: that one bucket is 400 of the real 420 observations (95%).
    assert volume_coverage["Q102"]["weekday"] == (400, 420)


def test_real_volume_coverage_excludes_the_2am_to_4am_window(tmp_path):
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        _insert_bucket(conn, "M60+", "weekday", n_observations=200, stop_id="s1", hour_bucket=2)
        _insert_bucket(conn, "M60+", "weekday", n_observations=80, stop_id="s2", hour_bucket=8)
    finally:
        conn.close()

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.bus_schedule_periods.effective_day_type_and_hour", side_effect=_identity_period_mapping):
        coverage = real_volume_coverage()

    assert coverage["M60+"]["weekday"] == (80, 80)


def test_real_volume_coverage_excludes_the_real_mapped_late_night_period_not_the_literal_hour(tmp_path):
    # Same 2026-10-25 fix as real_bucket_coverage's equivalent test, for
    # the volume-weighted view.
    db_path = tmp_path / "local_pipeline.sqlite3"
    conn = get_connection(str(db_path))
    try:
        _insert_bucket(conn, "B15", "weekend", n_observations=17, stop_id="s1", hour_bucket=1)
        _insert_bucket(conn, "B15", "weekend", n_observations=80, stop_id="s2", hour_bucket=8)
    finally:
        conn.close()

    def fake_mapping(route_id, day_type, hour):
        if hour in (2, 3):
            return (day_type, 1)
        return (day_type, hour)

    with patch("scripts.hourly_bus_sync.LOCAL_PIPELINE_DB_PATH", db_path), \
         patch("scripts.hourly_bus_sync.CORRIDORS", ["B15"]), \
         patch("scripts.hourly_bus_sync.bus_schedule_periods.effective_day_type_and_hour", side_effect=fake_mapping):
        coverage = real_volume_coverage()

    assert coverage["B15"]["weekend"] == (80, 80)


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


def test_coverage_complete_is_false_when_the_combined_pool_is_still_below_fraction():
    # 145/200 combined = 72.5%, still below the fraction even though it's
    # one pool now, not two separately-gated day types.
    coverage = {"M60+": {"weekday": (95, 100), "weekend": (50, 100)}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]):
        assert coverage_complete(coverage) is False


def test_coverage_complete_is_true_once_every_tracked_corridor_clears_the_fraction():
    coverage = {
        "M60+": {"weekday": (91, 100), "weekend": (90, 100)},  # exactly 90% combined
        "Q70+": {"weekday": (18, 20), "weekend": (19, 20)},
    }
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q70+"]):
        assert coverage_complete(coverage) is True


def test_coverage_complete_is_true_when_weekend_is_thin_but_combined_pool_clears(tmp_path):
    # 2026-10-12 (user): the whole point of folding equivalent-schedule
    # weekend hours into weekday is that a route shouldn't need its own
    # independently-sufficient weekend pool anymore. Weekday alone is
    # already well past 90%; weekend has a handful of real (divergent-
    # hour) buckets that haven't cleared, but they're a small enough
    # slice of the COMBINED pool not to drag the route below 90% overall
    # -- the old per-day-type gate would have wrongly failed this route.
    coverage = {"M60+": {"weekday": (190, 195), "weekend": (0, 5)}}  # 190/200 = 95% combined
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]):
        assert coverage_complete(coverage) is True


def test_check_and_maybe_stop_collection_writes_marker_only_when_complete():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.categorize_bus_data"), \
         patch("scripts.hourly_bus_sync.real_bucket_coverage",
               return_value={"M60+": {"weekday": (10, 100), "weekend": (10, 100)}}), \
         patch("scripts.hourly_bus_sync._write_remote_file", side_effect=lambda p, c: written.append((p, c))):
        stopped, coverage = check_and_maybe_stop_collection()
    assert stopped is False
    assert coverage == {"M60+": {"weekday": (10, 100), "weekend": (10, 100)}}
    # The day-type gate is always pushed (both day types still below the
    # completion fraction here, so M60+ stays needed on both) -- only the
    # full-stop marker is conditional.
    assert written == [(REMOTE_DAY_TYPE_GATE, json.dumps({"M60+": ["weekday", "weekend"]}))]


def test_check_and_maybe_stop_collection_writes_marker_when_coverage_complete():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.categorize_bus_data"), \
         patch("scripts.hourly_bus_sync.real_bucket_coverage",
               return_value={"M60+": {"weekday": (95, 100), "weekend": (95, 100)}}), \
         patch("scripts.hourly_bus_sync._write_remote_file", side_effect=lambda p, c: written.append((p, c))):
        stopped, coverage = check_and_maybe_stop_collection()
    assert stopped is True
    assert coverage == {"M60+": {"weekday": (95, 100), "weekend": (95, 100)}}
    # Gate push (M60+ now needs neither day type) then the full-stop marker.
    assert written == [
        (REMOTE_DAY_TYPE_GATE, json.dumps({"M60+": []})),
        (REMOTE_DONE_MARKER, json.dumps({"M60+": {"weekday": (95, 100), "weekend": (95, 100)}})),
    ]


def test_needed_day_types_keeps_both_needed_while_combined_pool_is_incomplete():
    # Route overall isn't combined-complete yet (135/300 = 45%), so
    # weekend's zero evidence must stay in the "needed" list rather than
    # being mistaken for "done" -- and weekday, despite its own figures
    # being far from 90%, is checked per-day-type here too.
    coverage = {"Q90": {"weekday": (70, 100), "weekend": (0, 0)}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["Q90"]):
        assert needed_day_types(coverage) == {"Q90": ["weekday", "weekend"]}


def test_needed_day_types_drops_a_day_type_once_it_clears_the_fraction():
    # Combined pool (135/300 = 45%) is still incomplete, so this falls
    # through to the per-day-type check: weekday is individually done
    # (95%), weekend lags (20%) -- the real current shape of every active
    # corridor (weekday 92-99%, weekend 55-84%, see CLAUDE.md 2026-11-16).
    coverage = {"Q90": {"weekday": (95, 100), "weekend": (40, 200)}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["Q90"]):
        assert needed_day_types(coverage) == {"Q90": ["weekend"]}


def test_needed_day_types_stops_both_once_the_combined_pool_clears_despite_a_thin_remainder():
    # Regression (code review, 2026-11-16): a route whose COMBINED pool
    # already clears the fraction (190/200 = 95%) must not stay gated to
    # collect weekend forever just because weekend's own thin remainder
    # (0/5) never clears individually -- the exact "double-penalizes a
    # thin remainder" problem coverage_complete() itself was redesigned
    # on 2026-10-12 to avoid (same fixture as
    # test_coverage_complete_is_true_when_weekend_is_thin_but_combined_pool_clears).
    coverage = {"M60+": {"weekday": (190, 195), "weekend": (0, 5)}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]):
        assert needed_day_types(coverage) == {"M60+": []}
