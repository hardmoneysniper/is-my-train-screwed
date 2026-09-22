"""Local reliability-data pipeline: run subway backfill/ingest + bus
derive + aggregation entirely on this machine, against a local sqlite DB.

Context (2026-09-22 cost incident, see CLAUDE.md): bus has no historical
backfill source -- it can only be captured by continuously polling the
live GTFS-RT feed as buses run, so it must keep running somewhere
always-on (Railway's backend service). Subway is the opposite: subwaydata.nyc
has a full historical archive, so there's no reason to burn Railway's
GB-RAM/disk-hours backfilling and ingesting it there. This script pulls
Railway's already-collected raw bus data down, backfills subway for the
same real-world window, and folds everything into reliability_buckets --
all locally, zero ongoing Railway cost.

Every step called here (download_subwaydata.run_backfill,
ingest_subwaydata.run_ingest, derive_bus_arrival_events.run_derive,
aggregate_reliability_buckets.run_aggregate) is already idempotent --
already-processed days/files are skipped. That means this script needs
no bespoke resume/checkpoint logic of its own: if it's interrupted
(Ctrl+C, machine sleep, crash), just re-run the same command and it picks
up exactly where it left off.

Usage:
    python scripts/run_local_pipeline.py
    python scripts/run_local_pipeline.py --skip-sync   # reuse already-synced bus data
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.realtime_proxy import SUBWAY_ZIP, TripIndex  # noqa: E402
from db import get_connection  # noqa: E402
from scripts.aggregate_reliability_buckets import run_aggregate  # noqa: E402
from scripts.derive_bus_arrival_events import run_derive as derive_bus_arrival_events_run_derive  # noqa: E402
from scripts.download_subwaydata import run_backfill as download_subwaydata_run_backfill  # noqa: E402
from scripts.ingest_subwaydata import (  # noqa: E402
    build_static_stop_times_index,
    run_ingest as ingest_subwaydata_run_ingest,
)

# Same Railway project this whole codebase already talks to (see
# CLAUDE.md and backup_from_railway.py for precedent of this exact
# project/service/environment triple).
PROJECT_ID = "77096939-d30b-46a4-b439-c545aff3fe25"
SERVICE_ID = "f66ebb7b-778d-4041-bde9-d66ce5c17223"  # backend
ENVIRONMENT_ID = "95c28f31-9ee7-465f-9046-034215422795"

DEFAULT_BUS_RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"
DEFAULT_SUBWAY_RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "subway"
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "local_pipeline.sqlite3"

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def sync_bus_data_from_railway(bus_raw_dir: Path) -> None:
    """Pull raw/bus/* from backend's Railway volume down to bus_raw_dir.

    Streams `tar -czf - -C /app/data/raw/bus .` over `railway ssh` (same
    technique as backup_from_railway.py) and extracts locally. Safe to
    re-run: tar extraction overwrites/adds files, and every downstream
    step is independently idempotent, so a repeated sync just re-fetches
    whatever's newest without corrupting anything already processed.
    """
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        raise RuntimeError("RAILWAY_API_TOKEN not set")

    railway_exe = shutil.which("railway")
    if railway_exe is None:
        raise RuntimeError("railway CLI not found on PATH")

    bus_raw_dir.mkdir(parents=True, exist_ok=True)
    # On Windows, npm installs `railway` as a .CMD shim -- subprocess can't
    # exec that directly without going through cmd.exe (same gotcha as
    # backup_from_railway.py).
    prefix = ["cmd", "/c"] if os.name == "nt" else []
    cmd = prefix + [
        railway_exe, "ssh",
        "-p", PROJECT_ID, "-s", SERVICE_ID, "-e", ENVIRONMENT_ID,
        "--", "tar", "-czf", "-", "-C", "/app/data/raw/bus", ".",
    ]
    print(f"[sync] pulling raw/bus from Railway to {bus_raw_dir}...", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"railway ssh failed (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}")

    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tar:
        tar.extractall(bus_raw_dir)
    print("[sync] done", flush=True)


def bus_date_range(bus_raw_dir: Path) -> tuple[date, date]:
    """Determine the real-world timeframe bus data was collected over,
    from raw filenames (YYYY-MM-DD.ndjson or YYYY-MM-DD.ndjson.gz)."""
    dates = []
    for path in bus_raw_dir.glob("*.ndjson*"):
        match = _DATE_RE.search(path.name)
        if match:
            dates.append(date.fromisoformat(match.group(1)))
    if not dates:
        raise RuntimeError(f"no bus raw files found in {bus_raw_dir}")
    return min(dates), max(dates)


def run_local_pipeline(
    bus_raw_dir: Path = DEFAULT_BUS_RAW_DIR,
    subway_raw_dir: Path = DEFAULT_SUBWAY_RAW_DIR,
    db_path: Path = DEFAULT_DB_PATH,
    skip_sync: bool = False,
) -> None:
    """Run the full reliability-data pipeline locally: sync bus data down
    from Railway, backfill+ingest subway data for the same real-world
    window bus was collected over, derive bus arrival events, and fold
    everything into reliability_buckets -- all against a local sqlite DB.
    """
    if not skip_sync:
        sync_bus_data_from_railway(bus_raw_dir)

    min_date, max_date = bus_date_range(bus_raw_dir)
    days = (max_date - min_date).days + 1
    print(f"[pipeline] bus data spans {min_date} to {max_date} ({days} days) -- "
          f"backfilling subway for the same window", flush=True)

    conn = get_connection(str(db_path))
    try:
        print("[pipeline] downloading subway backfill...", flush=True)
        download_subwaydata_run_backfill(subway_raw_dir, max_date, days=days)

        print("[pipeline] ingesting subway data...", flush=True)
        trip_index = TripIndex(SUBWAY_ZIP)
        static_index = build_static_stop_times_index(SUBWAY_ZIP)
        ingest_subwaydata_run_ingest(subway_raw_dir, conn, trip_index, static_index)

        print("[pipeline] deriving bus arrival events...", flush=True)
        derive_bus_arrival_events_run_derive(bus_raw_dir, conn)

        print("[pipeline] aggregating reliability_buckets...", flush=True)
        run_aggregate(conn)

        subway_n = conn.execute("SELECT COUNT(*) FROM arrival_events WHERE agency='subway'").fetchone()[0]
        bus_n = conn.execute("SELECT COUNT(*) FROM arrival_events WHERE agency='bus'").fetchone()[0]
        buckets_n = conn.execute("SELECT COUNT(*) FROM reliability_buckets").fetchone()[0]
        print(f"[pipeline] done. arrival_events: {subway_n} subway, {bus_n} bus. "
              f"reliability_buckets: {buckets_n} rows. DB at {db_path}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bus-raw-dir", type=Path, default=DEFAULT_BUS_RAW_DIR)
    parser.add_argument("--subway-raw-dir", type=Path, default=DEFAULT_SUBWAY_RAW_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Skip pulling bus data from Railway; use whatever's already in --bus-raw-dir",
    )
    args = parser.parse_args()
    run_local_pipeline(args.bus_raw_dir, args.subway_raw_dir, args.db_path, args.skip_sync)
