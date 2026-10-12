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
import gzip
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import bus_schedule_periods  # noqa: E402
from app.day_type import day_type_for  # noqa: E402
from app.risk_engine import MIN_N_OBSERVATIONS  # noqa: E402
from collectors.bus_collector import CORRIDORS  # noqa: E402
from db import get_connection  # noqa: E402
from scripts.aggregate_reliability_buckets import run_aggregate  # noqa: E402
from scripts.derive_bus_arrival_events import run_derive as derive_bus_arrival_events_run_derive  # noqa: E402

LOCAL_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"
LOCAL_PIPELINE_DB_PATH = Path(__file__).parent.parent / "data" / "local_pipeline.sqlite3"

PROJECT_ID = "77096939-d30b-46a4-b439-c545aff3fe25"
SERVICE_ID = "f66ebb7b-778d-4041-bde9-d66ce5c17223"  # backend
ENVIRONMENT_ID = "95c28f31-9ee7-465f-9046-034215422795"
REMOTE_BUS_DIR = "/app/data/raw/bus"
REMOTE_DONE_MARKER = "/app/data/.bus_collection_complete"

# Cost incident 2026-09-22, fixed 2026-09-28: this target-check used to
# live in backend/app/main.py, computed from Railway's OWN raw/bus
# directory -- but this script deletes every file from that same
# directory shortly after pulling it, so main.py's view of cumulative
# collection was being reset to near-zero every hour and could never
# reach a real target. This machine's LOCAL_DIR is the only durable,
# undeleted view of cumulative collection (nothing is ever deleted here)
# -- so the target-check now runs against LOCAL_DIR, and this script
# writes the stop marker to Railway once satisfied (main.py just watches
# for that marker, see its own module comment).
#
# 2026-10-12 CORRECTION (user-directed re-derivation): the original
# targets below (distinct_stops * 24h * 200 * 1.5) assumed raw 30s-poll
# records convert into derived reliability_buckets observations at
# roughly 1:1 (the "1.5" was meant as a safety margin on TOP of that).
# That assumption was wrong, confirmed by real measurement: all 4 active
# corridors (Q102 116%, Q72 106%, Q80 150%, Q90 101.5% of their OLD
# weekend targets) genuinely "completed" by that metric, yet
# reliability_buckets showed ZERO bus buckets anywhere -- weekday or
# weekend -- crossing n>=200 (max observed n was 80). Precisely measured
# the real conversion rate on Q90's single busiest real bucket (route
# Q90, stop_id 505438, weekday hour 5): 365 raw records in that specific
# (stop, hour) slice produced only 21 derived headway observations --
# because only 14 distinct VEHICLES passed that stop in that hour across
# ~15 weekdays of collection (most raw records are repeated predictions
# for the SAME vehicle approaching, not new events). Real ratio: ~17.4
# raw records per 1 unit of n_observations, not ~1.
#
# 2026-10-12 FINAL RESOLUTION: the raw-record-count proxy approach (both
# the original and the 17.4x-corrected version below) is retired as the
# stopping criterion. It failed twice for the same root reason: real
# per-bucket yield varies too much (even within one corridor) for any
# single "raw records needed" scalar to mean anything -- a literal
# recomputation attempt produced a 5-million-record target for one
# corridor, dominated by its single worst bucket. Separately, bus
# reliability_buckets moved from literal hour-of-day to real per-route
# SCHEDULED-headway periods (app/bus_schedule_periods.py) the same day,
# which alone produced a real ~10x jump in citable buckets -- making the
# whole premise of predicting raw-record needs from the OLD bucket
# structure moot anyway.
#
# Replaced with real_bucket_coverage() below: runs the actual local
# derive+aggregate pipeline (same one run_local_pipeline.py uses) against
# freshly-synced raw data every hour -- "categorizing" it, not just
# collecting it -- then measures the REAL fraction of each corridor's
# real (ever-observed) buckets that clear MIN_N_OBSERVATIONS directly
# from reliability_buckets. No more predicting; just measuring.
#
# BUS_COLLECTION_TARGETS is kept only as a cheap, zero-GTFS-parsing raw
# progress indicator for status-check logging (how much raw collection
# volume exists) -- it is NOT consulted for the stop decision anymore.
BUS_COLLECTION_TARGETS = {
    "Q70+": {"weekday": 584_640, "weekend": 584_640},
    "M60+": {"weekday": 3_173_760, "weekend": 3_173_760},
    "Q102": {"weekday": 2_672_640, "weekend": 2_672_640},
    "Q3": {"weekday": 6_596_640, "weekend": 6_596_640},
    "B15": {"weekday": 11_604_960, "weekend": 11_604_960},
    "Q10": {"weekday": 3_590_640, "weekend": 3_590_640},
    "Q72": {"weekday": 2_923_200, "weekend": 2_923_200},
    "Q80": {"weekday": 2_756_160, "weekend": 2_756_160},
    "Q90": {"weekday": 1_336_320, "weekend": 1_336_320},
}


def _railway_ssh(*remote_args: str, timeout: int = 120, input: bytes | None = None) -> subprocess.CompletedProcess:
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
        cmd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ,
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


def _write_remote_marker(content: str) -> None:
    result = _railway_ssh(
        "sh", "-c", f"mkdir -p /app/data && cat > {REMOTE_DONE_MARKER}",
        input=content.encode("utf-8"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"writing remote marker failed: {result.stderr.decode(errors='replace')}")


def local_route_day_type_counts() -> dict[str, dict[str, int]]:
    """Cumulative per-route record counts, split by day_type (weekday/
    weekend, see app/day_type.py), computed from LOCAL_DIR -- the only
    durable, never-deleted view of collection totals (see module
    docstring above)."""
    counts = {route: {"weekday": 0, "weekend": 0} for route in CORRIDORS}
    for path in sorted(LOCAL_DIR.glob("*.ndjson*")):
        service_date_str = path.name.split(".")[0]
        try:
            service_date = date.fromisoformat(service_date_str)
        except ValueError:
            continue
        day_type = day_type_for(service_date)
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt") as f:
                for line in f:
                    try:
                        route = json.loads(line).get("route_id")
                    except json.JSONDecodeError:
                        continue
                    if route in counts:
                        counts[route][day_type] += 1
        except OSError:
            continue
    return counts


# 2026-10-12 (user): a corridor/day_type is "ready to stop" once this
# fraction of its real (ever-observed, n_observations > 0) buckets clear
# MIN_N_OBSERVATIONS -- not 100%: some scheduled slots may see so little
# real traffic that chasing every last one isn't worth the wait (the
# same lesson as the retired raw-record-target attempts), and a bucket
# that's NEVER been observed even once gives no evidence either way, so
# it's excluded from the denominator rather than counted as "not ready".
COVERAGE_COMPLETION_FRACTION = 0.90


def categorize_bus_data() -> None:
    """Runs the real local pipeline (derive_bus_arrival_events +
    aggregate_reliability_buckets, same functions run_local_pipeline.py
    uses) against whatever raw data is on disk, folding it into
    local_pipeline.sqlite3's reliability_buckets. Both steps are
    idempotent (processed_days bookkeeping) -- safe to call every hour
    even when nothing new was synced; it just no-ops quickly. This is the
    "categorize" step (2026-10-12, user) -- turns raw collected data into
    real, query-ready reliability buckets on a regular cadence, not just
    once in a while."""
    conn = get_connection(str(LOCAL_PIPELINE_DB_PATH))
    try:
        derive_bus_arrival_events_run_derive(LOCAL_DIR, conn)
        run_aggregate(conn)
    finally:
        conn.close()


def real_bucket_coverage() -> dict[str, dict[str, tuple[int, int]]]:
    """{route: {day_type: (buckets_cleared, real_buckets_total)}} measured
    directly from local_pipeline.sqlite3's reliability_buckets -- the
    actual thing that matters (can get_risk cite from this bucket), not a
    raw-record proxy for it. Call categorize_bus_data() first so this
    reflects the latest synced data.

    Excludes hour_bucket < bus_schedule_periods.ANCHOR_HOUR (2026-10-12,
    user: "almost nobody rides a bus at 2am thus almost nobody is using
    our service" for it) -- risk_engine.py never cites these buckets
    (see its _is_late_night_bus), so tracking "coverage" for them here
    would just be noise that drags down a route's real completion
    status over slots nobody will ever see a probability for."""
    conn = get_connection(str(LOCAL_PIPELINE_DB_PATH))
    try:
        coverage: dict[str, dict[str, tuple[int, int]]] = {}
        for route in CORRIDORS:
            coverage[route] = {}
            for day_type in ("weekday", "weekend"):
                rows = conn.execute(
                    """
                    SELECT n_observations FROM reliability_buckets
                    WHERE agency = 'bus' AND route_id = ? AND day_type = ?
                      AND stat_type IN ('headway', 'prediction_error')
                      AND n_observations > 0
                      AND hour_bucket >= ?
                    """,
                    (route, day_type, bus_schedule_periods.ANCHOR_HOUR),
                ).fetchall()
                total = len(rows)
                cleared = sum(1 for r in rows if r[0] >= MIN_N_OBSERVATIONS)
                coverage[route][day_type] = (cleared, total)
        return coverage
    finally:
        conn.close()


def coverage_complete(coverage: dict[str, dict[str, tuple[int, int]]]) -> bool:
    """Every corridor in CORRIDORS must clear COVERAGE_COMPLETION_FRACTION
    for BOTH day types. A day_type with zero real (ever-observed) buckets
    is always "not ready" -- no evidence yet, not an automatic pass."""
    for route in CORRIDORS:
        for day_type in ("weekday", "weekend"):
            cleared, total = coverage.get(route, {}).get(day_type, (0, 0))
            if total == 0 or cleared / total < COVERAGE_COMPLETION_FRACTION:
                return False
    return True


def check_and_maybe_stop_collection() -> tuple[bool, dict[str, dict[str, tuple[int, int]]]]:
    """Categorizes the latest synced data, measures real bucket coverage,
    and writes the stop marker to Railway (main.py's own loop watches for
    it and cancels the collector) if every tracked corridor has cleared
    COVERAGE_COMPLETION_FRACTION for both day types. Returns (marker_
    written, coverage) -- the caller logs coverage either way, so a
    "still collecting" run still reports real per-corridor progress."""
    categorize_bus_data()
    coverage = real_bucket_coverage()
    if not coverage_complete(coverage):
        return False, coverage
    _write_remote_marker(json.dumps(coverage))
    return True, coverage


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

    try:
        stopped, coverage = check_and_maybe_stop_collection()
    except Exception as e:
        print(f"[hourly-bus-sync] categorize/coverage check failed: {e!r}", file=sys.stderr, flush=True)
        sys.exit(1)
    for route in CORRIDORS:
        for day_type in ("weekday", "weekend"):
            cleared, total = coverage.get(route, {}).get(day_type, (0, 0))
            pct = (cleared / total * 100) if total else 0.0
            print(f"[hourly-bus-sync] coverage {route} {day_type}: {cleared}/{total} buckets >= n={MIN_N_OBSERVATIONS} ({pct:.0f}%)", flush=True)
    if stopped:
        print("[hourly-bus-sync] all corridors cleared real bucket coverage, wrote stop marker to Railway", flush=True)
