"""Tests for scripts/hourly_bus_sync.py's real logic: pull-then-delete
safety ordering, append-vs-overwrite by file type, and the cumulative
per-corridor target-check that decides when to stop collection. railway
ssh itself is mocked -- not re-testing the CLI, testing that this script
uses it correctly and never deletes before a local write is confirmed.
Recurrence and overlap protection are handled by Windows Task Scheduler
(this script is one-shot, see its module docstring), not tested here."""
import gzip
import json
from unittest.mock import patch

from scripts.hourly_bus_sync import (
    all_targets_met,
    check_and_maybe_stop_collection,
    local_route_day_type_counts,
    sync_once,
)


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


def test_all_targets_met_is_false_for_a_corridor_with_no_target_entry():
    counts = {"M60+": {"weekday": 999_999, "weekend": 999_999}, "Q3": {"weekday": 999_999, "weekend": 999_999}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q3"]), \
         patch("scripts.hourly_bus_sync.BUS_COLLECTION_TARGETS", {"M60+": {"weekday": 100, "weekend": 100}}):
        # Q3 has real data but no target entry -- must not count as "met".
        assert all_targets_met(counts) is False


def test_all_targets_met_is_false_when_weekend_still_short():
    counts = {"M60+": {"weekday": 300, "weekend": 50}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.BUS_COLLECTION_TARGETS", {"M60+": {"weekday": 200, "weekend": 200}}):
        assert all_targets_met(counts) is False


def test_all_targets_met_is_true_once_every_tracked_corridor_hits_both_day_types():
    counts = {"M60+": {"weekday": 250, "weekend": 250}, "Q70+": {"weekday": 60, "weekend": 60}}
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+", "Q70+"]), \
         patch("scripts.hourly_bus_sync.BUS_COLLECTION_TARGETS", {
             "M60+": {"weekday": 200, "weekend": 200},
             "Q70+": {"weekday": 50, "weekend": 50},
         }):
        assert all_targets_met(counts) is True


def test_check_and_maybe_stop_collection_writes_marker_only_when_met():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.BUS_COLLECTION_TARGETS", {"M60+": {"weekday": 100, "weekend": 100}}), \
         patch("scripts.hourly_bus_sync.local_route_day_type_counts",
               return_value={"M60+": {"weekday": 50, "weekend": 50}}), \
         patch("scripts.hourly_bus_sync._write_remote_marker", side_effect=lambda c: written.append(c)):
        assert check_and_maybe_stop_collection() is False
    assert written == []


def test_check_and_maybe_stop_collection_writes_marker_when_targets_met():
    written = []
    with patch("scripts.hourly_bus_sync.CORRIDORS", ["M60+"]), \
         patch("scripts.hourly_bus_sync.BUS_COLLECTION_TARGETS", {"M60+": {"weekday": 100, "weekend": 100}}), \
         patch("scripts.hourly_bus_sync.local_route_day_type_counts",
               return_value={"M60+": {"weekday": 150, "weekend": 150}}), \
         patch("scripts.hourly_bus_sync._write_remote_marker", side_effect=lambda c: written.append(c)):
        assert check_and_maybe_stop_collection() is True
    assert len(written) == 1
    assert json.loads(written[0]) == {"M60+": {"weekday": 150, "weekend": 150}}
