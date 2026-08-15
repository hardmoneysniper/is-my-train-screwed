"""Pull a compressed snapshot of the Railway volume's collected data down
to this machine on a 5-day cadence.

Not a replacement for Railway's own volume persistence -- an independent
local copy in case of volume/project loss, account issues, or (though
unconfirmed either way) a write failure if the volume ever nears its cap.

Run via backend/run_railway_backup.bat, launched from the Windows Startup
folder (see CLAUDE.md) -- this account can't use Task Scheduler for real
interval scheduling, so instead this checks a local marker file every time
it's triggered (each logon) and only actually backs up once >=5 days have
passed since the last one.
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"
MARKER_PATH = Path(__file__).parent.parent / "data" / ".last_railway_backup"
BACKUP_INTERVAL_DAYS = 5

PROJECT_ID = "77096939-d30b-46a4-b439-c545aff3fe25"
SERVICE_ID = "91c6a5c2-7c04-46d0-9c01-24b17b9c4014"
ENVIRONMENT_ID = "95c28f31-9ee7-465f-9046-034215422795"


def _last_backup_time() -> datetime | None:
    if not MARKER_PATH.exists():
        return None
    try:
        return datetime.fromisoformat(MARKER_PATH.read_text().strip())
    except ValueError:
        return None


def _due() -> bool:
    last = _last_backup_time()
    return last is None or datetime.now(timezone.utc) - last >= timedelta(days=BACKUP_INTERVAL_DAYS)


def run_backup() -> None:
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        print("[backup] RAILWAY_API_TOKEN not set, aborting", file=sys.stderr, flush=True)
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    dest = BACKUP_DIR / f"railway-data-{timestamp}.tar.gz"

    railway_exe = shutil.which("railway")
    if railway_exe is None:
        print("[backup] railway CLI not found on PATH", file=sys.stderr, flush=True)
        sys.exit(1)
    # On Windows, npm installs `railway` as a .CMD shim -- subprocess can't
    # exec that directly without going through cmd.exe.
    prefix = ["cmd", "/c"] if os.name == "nt" else []
    cmd = prefix + [
        railway_exe, "ssh",
        "-p", PROJECT_ID, "-s", SERVICE_ID, "-e", ENVIRONMENT_ID,
        "--", "tar", "-czf", "-", "-C", "/app/data", "raw",
    ]

    print(f"[backup] {timestamp} starting backup to {dest}", flush=True)
    with open(dest, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=os.environ, timeout=300)

    ok = result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    if not ok:
        stderr = result.stderr.decode(errors="replace")
        print(f"[backup] FAILED (exit {result.returncode}): {stderr}", file=sys.stderr, flush=True)
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
        sys.exit(1)

    size_mb = dest.stat().st_size / 1_000_000
    print(f"[backup] OK: {dest.name} ({size_mb:.2f} MB)", flush=True)
    MARKER_PATH.write_text(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    if _due():
        run_backup()
    else:
        last = _last_backup_time()
        next_due = last + timedelta(days=BACKUP_INTERVAL_DAYS)
        print(f"[backup] not due yet (last: {last.isoformat()}, next due: {next_due.isoformat()})", flush=True)
