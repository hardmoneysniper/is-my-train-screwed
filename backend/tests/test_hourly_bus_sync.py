"""Tests for scripts/hourly_bus_sync.py's real logic: pull-then-delete
safety ordering and append-vs-overwrite by file type. railway ssh itself
is mocked -- not re-testing the CLI, testing that this script uses it
correctly and never deletes before a local write is confirmed. Recurrence
and overlap protection are handled by Windows Task Scheduler (this
script is one-shot, see its module docstring), not tested here."""
from unittest.mock import patch

from scripts.hourly_bus_sync import sync_once


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
