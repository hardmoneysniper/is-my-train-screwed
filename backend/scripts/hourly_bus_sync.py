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

from app.day_type import day_type_for  # noqa: E402
from collectors.bus_collector import CORRIDORS  # noqa: E402

LOCAL_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"

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
# Targets below are recomputed as distinct_stops * 24h * 200 (n-gate) *
# 17.4 (the measured real ratio, replacing the old formula's implicit
# ~1.5), using the same real stop counts as before (2026-10-01): Q70+=7,
# M60+=38, Q102=32, Q3=79, B15=139, Q10=43, Q72=35, Q80=33, Q90=16. This
# is still a rough, provisional correction, NOT independently re-measured
# per route -- it applies Q90's one measured ratio uniformly, and Q90 is
# a low-frequency route; a high-frequency trunk/SBS route (M60+, B15)
# likely has a meaningfully better real ratio that this overstates, while
# an even-lower-frequency stop/hour on some other route could understate
# it. At these corrected magnitudes, full completion for the slowest
# buckets is realistically months away, not weeks -- a genuinely open
# question (same family as the subway MIN_N_OBSERVATIONS=200 discussion)
# is whether bus should keep this per-hour bucket granularity and a
# uniform n=200 threshold at all, or use broader time-of-day buckets /a
# lower per-agency threshold instead. Not decided yet -- these numbers
# are "keep collecting, don't re-trigger prematurely," not a validated
# completion criterion.
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


def all_targets_met(counts: dict[str, dict[str, int]]) -> bool:
    """Every corridor in CORRIDORS must have a defined target in
    BUS_COLLECTION_TARGETS AND meet it, for both weekday and weekend. A
    corridor with no target entry yet (e.g. a newly-added one, see
    BUS_COLLECTION_TARGETS's comment) is always "not met" -- collection
    keeps running until real targets exist for every tracked corridor."""
    for route in CORRIDORS:
        targets = BUS_COLLECTION_TARGETS.get(route)
        if targets is None:
            return False
        for day_type, target in targets.items():
            if counts.get(route, {}).get(day_type, 0) < target:
                return False
    return True


def check_and_maybe_stop_collection() -> bool:
    """After a sync, checks cumulative local totals against
    BUS_COLLECTION_TARGETS; if every tracked corridor has met its target
    for both day types, writes the stop marker to Railway (main.py's own
    loop watches for it and cancels the collector). Returns True if the
    marker was (already, or just now) written."""
    counts = local_route_day_type_counts()
    if not all_targets_met(counts):
        return False
    _write_remote_marker(json.dumps(counts))
    return True


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
        stopped = check_and_maybe_stop_collection()
    except Exception as e:
        print(f"[hourly-bus-sync] collection target check failed: {e!r}", file=sys.stderr, flush=True)
        sys.exit(1)
    if stopped:
        print("[hourly-bus-sync] all corridor targets met, wrote stop marker to Railway", flush=True)
