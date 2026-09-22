"""Tests for scripts/hourly_bus_sync.py's real logic: pull-then-delete
safety ordering, append-vs-overwrite by file type, and lock staleness.
railway ssh itself is mocked -- not re-testing the CLI, testing that this
script uses it correctly and never deletes before a local write is
confirmed."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.hourly_bus_sync import _refresh_lock, sync_once


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


def test_refresh_lock_claims_when_no_existing_lock(tmp_path):
    lock_path = tmp_path / ".lock"
    with patch("scripts.hourly_bus_sync.LOCK_PATH", lock_path):
        assert _refresh_lock() is True
    assert lock_path.exists()


def test_refresh_lock_rejects_when_recently_held(tmp_path):
    lock_path = tmp_path / ".lock"
    lock_path.write_text(datetime.now(timezone.utc).isoformat())
    with patch("scripts.hourly_bus_sync.LOCK_PATH", lock_path):
        assert _refresh_lock() is False


def test_refresh_lock_self_heals_when_lock_is_stale(tmp_path):
    lock_path = tmp_path / ".lock"
    stale_time = datetime.now(timezone.utc) - timedelta(hours=3)
    lock_path.write_text(stale_time.isoformat())
    with patch("scripts.hourly_bus_sync.LOCK_PATH", lock_path):
        assert _refresh_lock() is True
