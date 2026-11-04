"""Pull a compressed snapshot of the Railway volume's collected data down
to this machine on a 3-day cadence.

Not a replacement for Railway's own volume persistence -- an independent
local copy in case of volume/project loss, account issues, or (though
unconfirmed either way) a write failure if the volume ever nears its cap.

Runs as a long-lived daemon (see run_daemon() below), launched once at
logon via backend/run_railway_backup.bat from the Windows Startup folder
(see CLAUDE.md -- this account can't use Task Scheduler). It then loops
on its own for as long as the machine stays on, re-checking by wall clock
whether a backup is due.

This replaces an earlier one-shot design that only checked at the moment
of a fresh logon and then exited -- on a laptop that sleeps/resumes
rather than fully logging out, that one-shot script never got a second
chance to run, and in practice went 8 days without a single backup
despite a working script and a correctly-installed Startup shortcut. A
persistent loop closes that gap: it keeps checking regardless of
sleep/wake cycles, and only genuinely needs a fresh launch after a real
shutdown or reboot (which Startup folder does cover).
"""
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"
MARKER_PATH = Path(__file__).parent.parent / "data" / ".last_railway_backup"
LOCK_PATH = Path(__file__).parent.parent / "data" / ".railway_backup_daemon.lock"

BACKUP_INTERVAL_DAYS = 3
CHECK_INTERVAL_SECONDS = 60 * 60  # re-check hourly -- a backup can only ever be up to
                                   # an hour later than the 3-day mark, and hourly wall-
                                   # clock checks cost nothing worth optimizing away
LOCK_STALE_AFTER = timedelta(hours=1)

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


def _refresh_lock() -> bool:
    """Claim (or renew) the daemon lock.

    Returns False only if another instance renewed the lock within the
    last hour -- i.e. a real second instance is active, not a leftover
    from a crashed/killed process (which self-heals once the lock goes
    stale).
    """
    now = datetime.now(timezone.utc)
    if LOCK_PATH.exists():
        try:
            lock_time = datetime.fromisoformat(LOCK_PATH.read_text().strip())
            if now - lock_time < LOCK_STALE_AFTER:
                return False
        except ValueError:
            pass
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(now.isoformat())
    return True


def run_backup() -> bool:
    """Attempt one backup. Returns True on success, False on failure.

    Never raises for expected failure modes (missing token, missing CLI,
    SSH failure, timeout) -- the daemon loop needs to survive a failed
    attempt and just retry on the next check, not crash.
    """
    token = os.environ.get("RAILWAY_API_TOKEN")
    if not token:
        print("[backup] RAILWAY_API_TOKEN not set, skipping this attempt", file=sys.stderr, flush=True)
        return False

    railway_exe = shutil.which("railway")
    if railway_exe is None:
        print("[backup] railway CLI not found on PATH, skipping this attempt", file=sys.stderr, flush=True)
        return False

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    dest = BACKUP_DIR / f"railway-data-{timestamp}.tar.gz"

    # On Windows, npm installs `railway` as a .CMD shim -- subprocess can't
    # exec that directly without going through cmd.exe.
    prefix = ["cmd", "/c"] if os.name == "nt" else []
    cmd = prefix + [
        railway_exe, "ssh",
        "-p", PROJECT_ID, "-s", SERVICE_ID, "-e", ENVIRONMENT_ID,
        "--", "tar", "-czf", "-", "-C", "/app/data", "raw",
    ]

    print(f"[backup] {timestamp} starting backup to {dest}", flush=True)
    try:
        with open(dest, "wb") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=os.environ, timeout=300)
    except subprocess.TimeoutExpired:
        print("[backup] FAILED: timed out after 300s", file=sys.stderr, flush=True)
        if dest.exists():
            dest.unlink()
        return False

    ok = result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    if not ok:
        stderr = result.stderr.decode(errors="replace")
        print(f"[backup] FAILED (exit {result.returncode}): {stderr}", file=sys.stderr, flush=True)
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
        return False

    size_mb = dest.stat().st_size / 1_000_000
    print(f"[backup] OK: {dest.name} ({size_mb:.2f} MB)", flush=True)
    MARKER_PATH.write_text(datetime.now(timezone.utc).isoformat())
    return True


def run_daemon() -> None:
    if not _refresh_lock():
        print("[backup-daemon] another instance already holds the lock, exiting", flush=True)
        return

    print(f"[backup-daemon] started -- checking every {CHECK_INTERVAL_SECONDS // 3600}h, "
          f"backing up every {BACKUP_INTERVAL_DAYS}d", flush=True)

    while True:
        _refresh_lock()
        if _due():
            try:
                run_backup()
            except Exception:
                print("[backup-daemon] unexpected error during backup attempt:", file=sys.stderr, flush=True)
                traceback.print_exc()
        else:
            last = _last_backup_time()
            next_due = last + timedelta(days=BACKUP_INTERVAL_DAYS) if last else None
            print(f"[backup-daemon] not due yet (last: {last}, next due: {next_due})", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_daemon()
