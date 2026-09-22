"""Tests for scripts/run_local_pipeline.py's real logic: determining the
collected bus timeframe from raw filenames. The sync/orchestration
functions are thin plumbing over already-tested functions (railway ssh,
download_subwaydata.run_backfill, ingest_subwaydata.run_ingest,
derive_bus_arrival_events.run_derive, aggregate_reliability_buckets.run_aggregate)
and external I/O -- not re-tested here."""
from datetime import date

import pytest

from scripts.run_local_pipeline import bus_date_range


def test_bus_date_range_finds_min_and_max_across_plain_and_gz_files(tmp_path):
    (tmp_path / "2026-08-15.ndjson.gz").write_text("")
    (tmp_path / "2026-09-01.ndjson.gz").write_text("")
    (tmp_path / "2026-09-22.ndjson").write_text("")  # today's file, not yet rotated

    min_date, max_date = bus_date_range(tmp_path)

    assert min_date == date(2026, 8, 15)
    assert max_date == date(2026, 9, 22)


def test_bus_date_range_single_file_returns_same_min_and_max(tmp_path):
    (tmp_path / "2026-09-10.ndjson.gz").write_text("")

    min_date, max_date = bus_date_range(tmp_path)

    assert min_date == max_date == date(2026, 9, 10)


def test_bus_date_range_raises_on_empty_directory(tmp_path):
    with pytest.raises(RuntimeError, match="no bus raw files found"):
        bus_date_range(tmp_path)


def test_bus_date_range_ignores_files_without_a_parseable_date(tmp_path):
    (tmp_path / "2026-09-10.ndjson.gz").write_text("")
    (tmp_path / "malformed.ndjson").write_text("no date in this filename")
    (tmp_path / "README.md").write_text("not a data file at all")

    min_date, max_date = bus_date_range(tmp_path)

    assert min_date == max_date == date(2026, 9, 10)
