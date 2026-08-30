"""backend/scripts/derive_bus_arrival_events.py

Derives `arrival_events` rows (spec §5.2) from the bus collector's raw
per-poll snapshots (`backend/collectors/bus_collector.py`'s output,
`backend/data/raw/bus/{date}.ndjson[.gz]`).

Unlike Task 3 (subwaydata.nyc, already-derived history), nobody has ever
turned these raw polls into arrival events before -- this is a genuinely
new algorithm: a per-vehicle approaching->past state machine over
`tripUpdates` polls. See `task-4-brief.md` for the full derivation,
verified directly against a real Railway-collected sample before writing
this (not just the spec's prose).

Key rules (see the brief for the full reasoning):
- `raw_source == "tripUpdates"` only. `vehiclePositions` records carry a
  single current/next stop, not the full remaining-stop-list this state
  machine depends on -- they are never parsed here.
- A stop disappearing from a vehicle's remaining-stop list between two
  polls is passage. `observed_arrival_ts` is the midpoint between the last
  poll where it was present and the first poll where it's absent.
- A >90s gap between those two polls marks the event `derivation_quality
  = "ambiguous"` (still emitted, never dropped).
- `predicted_arrival_ts_at_T_minus_5` is looked up from that stop's own
  prediction history (never fabricated from schedule): the poll closest
  to `observed_arrival_ts - 300s`, if within a 90s tolerance of that exact
  target; otherwise NULL.
- `scheduled_arrival_ts` / `delay_seconds` are unconditionally NULL for
  bus -- static-schedule matching was not asked for by the plan's Task 4
  text, and CLAUDE.md's minimalism rule means this task doesn't add it
  speculatively. (If bus delay-vs-schedule stats are wanted later, that's
  a distinct follow-up task, not implied here.)
- A stop still pending (never seen disappearing) at end-of-file is
  dropped, not emitted -- we never confirmed passage, so no event exists.
"""
import gzip
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from zoneinfo import ZoneInfo

from app.day_type import day_type_for
from db import get_connection

RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "bus"
LOCAL_TZ = ZoneInfo("America/New_York")

AMBIGUOUS_GAP_SECONDS = 90
PREDICTION_TOLERANCE_SECONDS = 90
PREDICTION_LOOKBACK_SECONDS = 300

# Every field this module's state machine reads off a tripUpdates record
# (final whole-branch review, Minor #4). A parseable-JSON line missing any
# of these would otherwise crash the whole day's processing via direct
# dict indexing (e.g. record["route_id"]) -- unlike a malformed-JSON line,
# which is already skipped gracefully below. Low real-world likelihood
# (the production collector always emits these), but this runs unattended
# over weeks of backfill data, so skip-and-log rather than crash, matching
# the malformed-line handling's spirit. `predicted_arrival_ts` is
# deliberately excluded -- it's genuinely optional (`.get(...)` with a
# None fallback is the correct, existing behavior for it).
REQUIRED_TRIP_UPDATE_FIELDS = ("polled_at", "route_id", "direction", "trip_id", "vehicle_id", "stop_id")


@dataclass
class _TrackedStop:
    """State for one (vehicle_id, stop_id) pair currently believed to be in
    a vehicle's remaining-stop list. route_id/direction/trip_id are captured
    once, when tracking starts, and never overwritten -- this is what makes
    a trip_id change mid-file resolve correctly (the OLD trip's identity
    survives onto the emitted event for a stop that was already pending
    when the change happened)."""
    route_id: str
    direction: int
    trip_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    prediction_history: list = field(default_factory=list)  # list[(datetime, int | None)]


def already_ingested(conn, service_date: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM arrival_events WHERE agency = 'bus' AND service_date = ? LIMIT 1",
        (service_date.isoformat(),),
    ).fetchone()
    return row is not None


def _open_ndjson(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _iter_trip_update_records(path: Path):
    """Stream `path` line by line, yielding parsed dicts for tripUpdates
    records only, in file order. Unparseable lines are skipped with a
    logged warning rather than crashing the whole day."""
    with _open_ndjson(path) as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"[derive_bus_arrival_events] {path.name}:{line_num} "
                    f"unparseable line, skipping: {exc}",
                    file=sys.stderr,
                )
                continue
            if record.get("raw_source") != "tripUpdates":
                continue
            missing = [f for f in REQUIRED_TRIP_UPDATE_FIELDS if record.get(f) is None]
            if missing:
                print(
                    f"[derive_bus_arrival_events] {path.name}:{line_num} "
                    f"tripUpdates record missing field(s) {missing}, skipping",
                    file=sys.stderr,
                )
                continue
            yield record


def _midpoint(a: datetime, b: datetime) -> datetime:
    return a + (b - a) / 2


def _closest_prediction(prediction_history: list, target: datetime):
    """Return the predicted_arrival_ts (may itself be None) of the history
    entry whose polled_at is closest to `target`, if that closest entry is
    within PREDICTION_TOLERANCE_SECONDS of it. Otherwise None (never picks
    a stale value just because it's the closest available)."""
    if not prediction_history:
        return None
    closest_polled_at, closest_predicted = min(
        prediction_history, key=lambda entry: abs((entry[0] - target).total_seconds())
    )
    if abs((closest_polled_at - target).total_seconds()) <= PREDICTION_TOLERANCE_SECONDS:
        return closest_predicted
    return None


def _epoch_to_iso(epoch) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _build_event(
    vehicle_id: str,
    stop_id: str,
    tracked: _TrackedStop,
    passed_at: datetime,
    service_date: date,
) -> tuple[dict, str]:
    observed_arrival_dt = _midpoint(tracked.last_seen_at, passed_at)
    gap_seconds = (passed_at - tracked.last_seen_at).total_seconds()
    quality = "ambiguous" if gap_seconds > AMBIGUOUS_GAP_SECONDS else "clean"

    target = observed_arrival_dt - timedelta(seconds=PREDICTION_LOOKBACK_SECONDS)
    predicted_epoch = _closest_prediction(tracked.prediction_history, target)

    event = {
        "agency": "bus",
        "route_id": tracked.route_id,
        "direction": str(tracked.direction),
        "stop_id": stop_id,
        "vehicle_id": vehicle_id,
        "trip_id": tracked.trip_id,
        "observed_arrival_ts": observed_arrival_dt.isoformat(),
        "scheduled_arrival_ts": None,
        "delay_seconds": None,
        "predicted_arrival_ts_at_T_minus_5": _epoch_to_iso(predicted_epoch),
        "service_date": service_date.isoformat(),
        "day_type": day_type_for(service_date),
        "hour_bucket": observed_arrival_dt.astimezone(LOCAL_TZ).hour,
        "derivation_quality": quality,
    }
    return event, quality


def _insert_events(conn, events: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO arrival_events (
            agency, route_id, direction, stop_id, vehicle_id, trip_id,
            observed_arrival_ts, scheduled_arrival_ts, delay_seconds,
            predicted_arrival_ts_at_T_minus_5, service_date, day_type,
            hour_bucket, derivation_quality
        ) VALUES (
            :agency, :route_id, :direction, :stop_id, :vehicle_id, :trip_id,
            :observed_arrival_ts, :scheduled_arrival_ts, :delay_seconds,
            :predicted_arrival_ts_at_T_minus_5, :service_date, :day_type,
            :hour_bucket, :derivation_quality
        )
        """,
        events,
    )
    conn.commit()


def process_day(conn, raw_path: Path, service_date: date) -> None:
    if already_ingested(conn, service_date):
        print(f"[derive_bus_arrival_events] {service_date} -> skipped (already ingested)")
        return

    # state[vehicle_id][stop_id] -> _TrackedStop, for stops currently
    # believed pending (seen in some poll, not yet seen disappearing).
    state: dict[str, dict[str, _TrackedStop]] = {}
    events: list[dict] = []
    n_poll_cycles = 0
    n_clean = 0
    n_ambiguous = 0

    for polled_at_str, group in groupby(
        _iter_trip_update_records(raw_path), key=lambda r: r["polled_at"]
    ):
        n_poll_cycles += 1
        poll_time = datetime.fromisoformat(polled_at_str)

        # This poll cycle's records, grouped by vehicle_id -> {stop_id: record}.
        by_vehicle: dict[str, dict[str, dict]] = {}
        for record in group:
            vehicle_id = record.get("vehicle_id")
            stop_id = record.get("stop_id")
            if vehicle_id is None or stop_id is None:
                continue
            by_vehicle.setdefault(vehicle_id, {})[stop_id] = record

        # Only vehicles present in THIS poll cycle are touched. A vehicle
        # entirely absent from a cycle (e.g. mid poll-failure gap) is left
        # untouched -- its pending stops carry over unchanged until it
        # reappears (or get dropped at EOF if it never does).
        for vehicle_id, stops_this_poll in by_vehicle.items():
            tracked = state.setdefault(vehicle_id, {})
            current_stop_ids = set(stops_this_poll.keys())
            tracked_stop_ids = set(tracked.keys())

            for stop_id in current_stop_ids - tracked_stop_ids:
                record = stops_this_poll[stop_id]
                tracked[stop_id] = _TrackedStop(
                    route_id=record["route_id"],
                    direction=record["direction"],
                    trip_id=record["trip_id"],
                    first_seen_at=poll_time,
                    last_seen_at=poll_time,
                    prediction_history=[(poll_time, record.get("predicted_arrival_ts"))],
                )

            for stop_id in current_stop_ids & tracked_stop_ids:
                record = stops_this_poll[stop_id]
                t = tracked[stop_id]
                t.last_seen_at = poll_time
                t.prediction_history.append((poll_time, record.get("predicted_arrival_ts")))
                # route_id/direction/trip_id deliberately NOT overwritten.

            for stop_id in tracked_stop_ids - current_stop_ids:
                t = tracked.pop(stop_id)
                event, quality = _build_event(
                    vehicle_id, stop_id, t, passed_at=poll_time, service_date=service_date
                )
                events.append(event)
                if quality == "ambiguous":
                    n_ambiguous += 1
                else:
                    n_clean += 1

    n_pending_dropped = sum(len(stops) for stops in state.values())

    if events:
        _insert_events(conn, events)

    print(
        f"[derive_bus_arrival_events] {service_date} -> poll_cycles={n_poll_cycles} "
        f"events_clean={n_clean} events_ambiguous={n_ambiguous} "
        f"pending_dropped={n_pending_dropped}"
    )


def _service_date_from_filename(path: Path) -> date:
    name = path.name
    if name.endswith(".ndjson.gz"):
        date_str = name[: -len(".ndjson.gz")]
    elif name.endswith(".ndjson"):
        date_str = name[: -len(".ndjson")]
    else:
        raise ValueError(f"unrecognized bus raw filename: {name}")
    return date.fromisoformat(date_str)


def run_derive(raw_dir: Path, conn) -> None:
    paths = list(raw_dir.glob("*.ndjson")) + list(raw_dir.glob("*.ndjson.gz"))
    for path in sorted(paths, key=_service_date_from_filename):
        service_date = _service_date_from_filename(path)
        process_day(conn, path, service_date)


if __name__ == "__main__":
    conn = get_connection()
    try:
        run_derive(RAW_DIR, conn)
    except Exception as exc:
        print(f"[derive_bus_arrival_events] fatal: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
