"""Pull collected bus raw data down from Railway's backend volume to this
machine every hour, deleting each file from Railway right after it's
safely copied locally -- keeps Railway's disk footprint small and moves
the growing historical record onto the user's own machine instead
(2026-09-22 cost incident, see CLAUDE.md).

Safe by construction: bus_collector.py's run_forever() reopens today's
target file in APPEND mode every ~30s poll cycle (see
collectors/bus_collector.py) rather than holding a persistent file
handle -- deleting today's in-progress file mid-day is safe, the very
next poll cycle just creates a fresh file at the same path and keeps
appending. A file is only deleted from Railway AFTER its bytes are
confirmed written locally (size-checked), never before.

Today's in-progress .ndjson file reappears (empty, then refilling) after
each deletion -- its content is APPENDED to the matching local file
across sync cycles, reconstructing the full day incrementally. Already-
rotated .gz files are one-shot (created once, at day rollover) and are
written fresh locally.

Runs as a long-lived daemon (see run_daemon() below), launched once at
logon via backend/run_hourly_bus_sync.bat from the Windows Startup
folder (Task Scheduler is blocked on this account -- see CLAUDE.md),
same pattern as backup_from_railway.py.
"""
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

LOCAL_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"
LOCK_PATH = Path(__file__).parent.parent / "data" / ".hourly_bus_sync_daemon.lock"

SYNC_INTERVAL_SECONDS = 60 * 60
LOCK_STALE_AFTER_SECONDS = 60 * 60 * 2

PROJECT_ID = "77096939-d30b-46a4-b439-c545aff3fe25"
SERVICE_ID = "f66ebb7b-778d-4041-bde9-d66ce5c17223"  # backend
ENVIRONMENT_ID = "95c28f31-9ee7-465f-9046-034215422795"
REMOTE_BUS_DIR = "/app/data/raw/bus"


def _railway_ssh(*remote_args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    railway_exe = shutil.which("railway")
    if railway_exe is None:
        raise RuntimeError("railway CLI not found on PATH")
    # On Windows, npm installs `railway` as a .CMD shim -- subprocess can't
    # exec that directly without going through cmd.exe (same gotcha as
    # backup_from_railway.py).
    prefix = ["cmd", "/c"] if os.name == "nt" else []
    cmd = prefix + [
        railway_exe, "ssh",
        "-p", PROJECT_ID, "-s", SERVICE_ID, "-e", ENVIRONMENT_ID,
        "--", *remote_args,
    ]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ, timeout=timeout)


def _list_remote_files() -> list[str]:
    result = _railway_ssh("sh", "-c", f"ls -1 {REMOTE_BUS_DIR} 2>/dev/null || true")
    if result.returncode != 0:
        raise RuntimeError(f"listing remote files failed: {result.stderr.decode(errors='replace')}")
    names = result.stdout.decode(errors="replace").splitlines()
    return [n.strip() for n in names if n.strip()]


def _pull_remote_file(filename: str) -> bytes:
    result = _railway_ssh("cat", f"{REMOTE_BUS_DIR}/{filename}", timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"pulling {filename} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _delete_remote_file(filename: str) -> None:
    result = _railway_ssh("rm", "-f", f"{REMOTE_BUS_DIR}/{filename}")
    if result.returncode != 0:
        raise RuntimeError(f"deleting {filename} failed: {result.stderr.decode(errors='replace')}")


def sync_once() -> dict[str, int]:
    """One sync cycle: list remote raw/bus files, pull each down (writing
    fresh for .gz files, appending for today's in-progress .ndjson file
    since it can reappear across cycles), then delete it from Railway
    only after the local write is confirmed. Returns {filename: bytes
    pulled} for logging/testing. Empty remote files are skipped (nothing
    new since the last cycle) and left alone -- deleting a 0-byte file
    the collector hasn't written to yet would just make it recreate one,
    no benefit."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    pulled: dict[str, int] = {}
    for filename in _list_remote_files():
        content = _pull_remote_file(filename)
        if len(content) == 0:
            continue
        local_path = LOCAL_DIR / filename
        mode = "ab" if filename.endswith(".ndjson") else "wb"
        with open(local_path, mode) as f:
            f.write(content)
        written = local_path.stat().st_size
        if written == 0:
            raise RuntimeError(f"local write for {filename} produced an empty file, refusing to delete remote copy")
        _delete_remote_file(filename)
        pulled[filename] = len(content)
    return pulled


def _refresh_lock() -> bool:
    """Claim (or renew) the daemon lock. Returns False only if another
    instance renewed the lock within the last 2 hours -- i.e. a real
    second instance is active, not a leftover from a crashed/killed
    process (which self-heals once the lock goes stale), matching
    backup_from_railway.py's established pattern."""
    now = datetime.now(timezone.utc)
    if LOCK_PATH.exists():
        try:
            lock_time = datetime.fromisoformat(LOCK_PATH.read_text().strip())
            if (now - lock_time).total_seconds() < LOCK_STALE_AFTER_SECONDS:
                return False
        except ValueError:
            pass
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(now.isoformat())
    return True


def run_daemon() -> None:
    if not _refresh_lock():
        print("[hourly-bus-sync] another instance already holds the lock, exiting", flush=True)
        return

    print(f"[hourly-bus-sync] started -- syncing every {SYNC_INTERVAL_SECONDS // 60}min", flush=True)
    while True:
        _refresh_lock()
        try:
            pulled = sync_once()
            if pulled:
                print(f"[hourly-bus-sync] {datetime.now(timezone.utc).isoformat()} pulled {pulled}", flush=True)
            else:
                print(f"[hourly-bus-sync] {datetime.now(timezone.utc).isoformat()} nothing new", flush=True)
        except Exception:
            print("[hourly-bus-sync] sync cycle failed:", file=sys.stderr, flush=True)
            traceback.print_exc()
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_daemon()
