"""Bus GTFS-RT raw-snapshot collector — Q70/M60 seed corridors (spec §2, §5.2).

Polls MTA's public bus GTFS-RT feed (gtfsrt.prod.obanyc.com) every 30s,
filters to the configured corridors, and appends raw polling snapshots to
daily-rotated ndjson files. Per spec §5.2, only raw snapshots are written
here — arrival-event derivation (the approaching->past state machine) and
aggregation into reliability_buckets happen later, offline, by replaying
these files. This collector's only job is to run uninterrupted starting
now, since bus reliability has no retroactive historical source (spec §4)
and needs ~2-3 weeks of real observations before Phase 2's probabilities
are meaningful.

Route IDs and full field shapes (trip_update / vehicle position) confirmed
live against the real feed on 2026-08-14 — see REUSE.md §5 for why GTFS-RT
was chosen over SIRI StopMonitoring (mini-nyc-3d has a reusable decode
pattern for GTFS-RT; nothing to port for SIRI).
"""
import gzip
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from google.transit import gtfs_realtime_pb2

CORRIDORS = ["Q70+", "M60+", "Q102"]  # spec §2 seed set + Q102 (user, 2026-08-29); Roosevelt
# Island Tram is static-schedule, not collected here. Q102's real route_id confirmed live
# as "Q102" -- no SBS "+" suffix, unlike Q70+/M60+ (checked directly, not assumed from
# that pattern; not every route carries one).
# Note: the M subway line (as in an M-train -> Q70 transfer) is NOT added here --
# subway isn't corridor-filtered like bus. It's covered system-wide by the Phase 2
# subwaydata.nyc ingestion pipeline instead (see docs/superpowers/plans/
# 2026-08-27-phase-2-risk-engine-plan.md), and get_risk composes any transfer
# pair (M->Q70 included) at query time from each route's own bucket -- spec §5
# explicitly never stores per-transfer-pair data.
POLL_INTERVAL_SECONDS = 60  # was 30 (2026-08-15 - 2026-08-27); halved 2026-08-27 to cut
# data volume roughly in half (measured ~570-695K raw records/day at 30s) while
# staying well under spec §5.2's 90s ambiguity threshold for passage-moment
# derivation -- 60s leaves real margin against jitter/retries, 90s exactly
# would leave none.
DATA_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"
BASE_URL = "https://gtfsrt.prod.obanyc.com"
ENDPOINTS = ["tripUpdates", "vehiclePositions"]
MAX_BACKOFF_SECONDS = 300

_last_seen_date_str: str | None = None  # tracks day rollover so we compress exactly once per boundary


def _api_key() -> str:
    key = os.environ.get("MTA_BUSTIME_API_KEY")
    if not key:
        raise RuntimeError("MTA_BUSTIME_API_KEY not set")
    return key


def _fetch_feed(endpoint: str, key: str) -> gtfs_realtime_pb2.FeedMessage:
    url = f"{BASE_URL}/{endpoint}?key={key}"
    with urllib.request.urlopen(url, timeout=20) as response:
        data = response.read()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    return feed


def _compress_and_remove(path: Path) -> None:
    """Gzip a completed day's ndjson file and remove the plaintext original.

    Today's file is never touched here — only files from a prior day, so a
    file being actively appended to is never compressed out from under the
    writer.
    """
    gz_path = path.with_suffix(path.suffix + ".gz")
    original_size = path.stat().st_size
    try:
        with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        compressed_size = gz_path.stat().st_size
        path.unlink()
        ratio = compressed_size / original_size if original_size else 0
        print(f"[bus_collector] compressed {path.name}: {original_size} -> {compressed_size} bytes "
              f"({ratio:.1%})", flush=True)
    except Exception:
        print(f"[bus_collector] failed to compress {path.name}:", file=sys.stderr, flush=True)
        traceback.print_exc()


def _compress_stale_files(today_str: str) -> None:
    """Gzip any *.ndjson file in DATA_DIR that isn't today's — covers both a
    normal day-boundary rollover and any file left uncompressed by a prior
    run that was restarted mid-day (e.g. after a Startup-folder relaunch).
    """
    if not DATA_DIR.exists():
        return
    for path in DATA_DIR.glob("*.ndjson"):
        if path.stem != today_str:
            threading.Thread(target=_compress_and_remove, args=(path,), daemon=True).start()


def _rotated_path(now: datetime) -> Path:
    global _last_seen_date_str
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today_str = now.strftime('%Y-%m-%d')
    if _last_seen_date_str != today_str:
        _compress_stale_files(today_str)
        _last_seen_date_str = today_str
    return DATA_DIR / f"{today_str}.ndjson"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_trip_update(entity, polled_at: str) -> list[dict]:
    trip = entity.trip_update.trip
    if trip.route_id not in CORRIDORS:
        return []
    vehicle_id = entity.trip_update.vehicle.id if entity.trip_update.HasField("vehicle") else None
    records = []
    for stu in entity.trip_update.stop_time_update:
        predicted = None
        if stu.HasField("arrival") and stu.arrival.time:
            predicted = stu.arrival.time
        elif stu.HasField("departure") and stu.departure.time:
            predicted = stu.departure.time
        records.append({
            "polled_at": polled_at,
            "agency": "MTA",
            "route_id": trip.route_id,
            "direction": trip.direction_id,
            "stop_id": stu.stop_id,
            "vehicle_id": vehicle_id,
            "trip_id": trip.trip_id,
            "predicted_arrival_ts": predicted,
            "distance_along_route": None,
            "raw_source": "tripUpdates",
        })
    return records


def _record_from_vehicle_position(entity, polled_at: str) -> list[dict]:
    trip = entity.vehicle.trip
    if trip.route_id not in CORRIDORS:
        return []
    has_position = entity.vehicle.HasField("position")
    return [{
        "polled_at": polled_at,
        "agency": "MTA",
        "route_id": trip.route_id,
        "direction": trip.direction_id,
        "stop_id": entity.vehicle.stop_id or None,
        "vehicle_id": entity.vehicle.vehicle.id if entity.vehicle.HasField("vehicle") else None,
        "trip_id": trip.trip_id,
        "predicted_arrival_ts": None,
        "distance_along_route": None,
        "raw_source": "vehiclePositions",
        "lat": entity.vehicle.position.latitude if has_position else None,
        "lon": entity.vehicle.position.longitude if has_position else None,
    }]


def poll_once(key: str) -> list[dict]:
    records = []
    for endpoint in ENDPOINTS:
        feed = _fetch_feed(endpoint, key)
        polled_at = _now_iso()
        for entity in feed.entity:
            if endpoint == "tripUpdates" and entity.HasField("trip_update"):
                records.extend(_record_from_trip_update(entity, polled_at))
            elif endpoint == "vehiclePositions" and entity.HasField("vehicle"):
                records.extend(_record_from_vehicle_position(entity, polled_at))
    return records


def run_forever():
    key = _api_key()
    backoff = POLL_INTERVAL_SECONDS
    print(f"[bus_collector] starting, corridors={CORRIDORS}, interval={POLL_INTERVAL_SECONDS}s, "
          f"writing to {DATA_DIR}", flush=True)
    while True:
        cycle_start = time.monotonic()
        try:
            records = poll_once(key)
            if records:
                path = _rotated_path(datetime.now(timezone.utc))
                with open(path, "a", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r) + "\n")
            print(f"[bus_collector] {_now_iso()} wrote {len(records)} records", flush=True)
            backoff = POLL_INTERVAL_SECONDS
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"[bus_collector] poll failed: {e!r}, backing off {backoff}s", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue
        except Exception:
            print("[bus_collector] unexpected error:", file=sys.stderr, flush=True)
            traceback.print_exc()
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    run_forever()
