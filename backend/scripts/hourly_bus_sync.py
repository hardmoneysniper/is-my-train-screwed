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

Runs as a one-shot script: does one sync cycle, then exits. Recurrence
and overlap protection are both handled natively by Windows Task
Scheduler (see CLAUDE.md -- "at logon" triggers are blocked on this
account, likely an anti-persistence policy, but genuine recurring
triggers like `/sc hourly` work fine and are a better fit than a
self-looping daemon anyway: Task Scheduler's own "don't start a new
instance if already running" setting replaces the need for this
script to manage its own lock file).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

LOCAL_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"

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
    # Windows Task Scheduler-launched processes can deliver a spurious
    # Ctrl+C to child processes when the parent console/session tears
    # down -- isolating this subprocess into its own process group stops
    # that signal from propagating to it.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ,
        timeout=timeout, creationflags=creationflags,
    )


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


if __name__ == "__main__":
    try:
        pulled = sync_once()
    except Exception as e:
        print(f"[hourly-bus-sync] sync cycle failed: {e!r}", file=sys.stderr, flush=True)
        sys.exit(1)
    if pulled:
        print(f"[hourly-bus-sync] pulled {pulled}", flush=True)
    else:
        print("[hourly-bus-sync] nothing new", flush=True)
