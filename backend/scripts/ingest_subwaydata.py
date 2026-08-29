"""backend/scripts/ingest_subwaydata.py

Parses subwaydata.nyc daily archives (Task 2's `download_subwaydata.py`
output, `backend/data/raw/subway/{date}.tar.xz`) into `arrival_events` rows
(spec §5.2), matching each day's RT-style trip_ids against the static
subway GTFS via `TripIndex` (built in Phase 1 Task 11 for the real-time
proxy, reused as-is here -- see `app/realtime_proxy.py`).

Backfill-only: this ingests already-observed history. It is not a live
collector -- OTP + the existing realtime proxy already serve live
predictions directly to users -- so `predicted_arrival_ts_at_T_minus_5` is
always NULL for subway-derived rows (subwaydata.nyc's schema has no
"predicted 5 minutes prior" field to draw one from).

Unmatched trip_ids (~10-25%, per Task 11's measured suffix-match rates)
are skipped entirely, not fabricated -- see task-3-brief.md step 3c.
"""
import csv
import io
import sys
import tarfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.realtime_proxy import SUBWAY_ZIP, TripIndex
from db import get_connection

RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "subway"
LOCAL_TZ = ZoneInfo("America/New_York")

# Major US federal holidays, 2025-2026 (this project's active window).
# Plain hardcoded set, not a recurring-holiday calculation library --
# per task-3-brief.md step 3i.
HOLIDAYS = {
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents Day
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 10, 13), # Columbus Day
    date(2025, 11, 11), # Veterans Day
    date(2025, 11, 27), # Thanksgiving Day
    date(2025, 12, 25), # Christmas Day
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 4),   # Independence Day
    date(2026, 9, 7),   # Labor Day
    date(2026, 10, 12), # Columbus Day
    date(2026, 11, 11), # Veterans Day
    date(2026, 11, 26), # Thanksgiving Day
    date(2026, 12, 25), # Christmas Day
}


def day_type_for(service_date: date) -> str:
    if service_date.weekday() >= 5 or service_date in HOLIDAYS:
        return "weekend"
    return "weekday"


def build_static_stop_times_index(gtfs_zip_path: Path) -> dict[tuple[str, str], str]:
    """(static trip_id, stop_id) -> extended HH:MM:SS arrival_time string.

    Loaded once at script start (565,094 rows in the real subway.zip) and
    reused across every day processed -- never rebuilt per day or per row.
    """
    import zipfile

    index: dict[tuple[str, str], str] = {}
    with zipfile.ZipFile(gtfs_zip_path) as z:
        with z.open("stop_times.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                index[(row["trip_id"], row["stop_id"])] = row["arrival_time"]
    return index


def parse_gtfs_time(time_str: str) -> timedelta:
    """Parse GTFS's extended HH:MM:SS (can exceed 24:00:00 for trips that
    run past midnight relative to their service day) as an offset from
    local midnight -- never as a `time`, which can't represent >= 24h."""
    hours, minutes, seconds = (int(part) for part in time_str.split(":"))
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def scheduled_ts_utc(service_date: date, time_str: str) -> datetime:
    """Combine a service date's local (America/New_York) midnight with a
    GTFS extended time-of-day offset, returning an absolute UTC datetime."""
    local_midnight = datetime(
        service_date.year, service_date.month, service_date.day, tzinfo=LOCAL_TZ
    )
    local_dt = local_midnight + parse_gtfs_time(time_str)
    return local_dt.astimezone(timezone.utc)


def _find_member(tar: tarfile.TarFile, suffix: str) -> tarfile.TarInfo:
    for member in tar.getmembers():
        if member.name.endswith(suffix):
            return member
    raise FileNotFoundError(f"no archive member ending with {suffix!r}")


def already_ingested(conn, service_date: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM arrival_events WHERE agency = 'subway' AND service_date = ? LIMIT 1",
        (service_date.isoformat(),),
    ).fetchone()
    return row is not None


def process_day(
    conn,
    tar_path: Path,
    service_date: date,
    trip_index: TripIndex,
    static_index: dict[tuple[str, str], str],
) -> None:
    if already_ingested(conn, service_date):
        print(f"[ingest_subwaydata] {service_date} -> skipped (already ingested)")
        return

    start_date_str = service_date.strftime("%Y%m%d")

    with tarfile.open(tar_path, "r:xz") as tar:
        trips_member = _find_member(tar, "_trips.csv")
        stop_times_member = _find_member(tar, "_stop_times.csv")

        trips_by_uid: dict[str, dict] = {}
        with tar.extractfile(trips_member) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                trips_by_uid[row["trip_uid"]] = {
                    "trip_id": row["trip_id"],
                    "route_id": row["route_id"],
                    "direction_id": row["direction_id"],
                    "vehicle_id": row["vehicle_id"],
                }

        rows_read = 0
        skipped_trip_uid_not_found = 0
        skipped_no_timestamp = 0
        skipped_unmatched_trip = 0
        events: list[dict] = []

        with tar.extractfile(stop_times_member) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                rows_read += 1

                trip = trips_by_uid.get(row["trip_uid"])
                if trip is None:
                    skipped_trip_uid_not_found += 1
                    continue

                raw_ts = row["arrival_time"] or row["departure_time"]
                if not raw_ts:
                    skipped_no_timestamp += 1
                    continue
                observed_arrival_ts = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)

                resolved_trip_id = trip_index.resolve(trip["trip_id"], start_date_str)
                if resolved_trip_id is None:
                    skipped_unmatched_trip += 1
                    continue

                static_time_str = static_index.get((resolved_trip_id, row["stop_id"]))
                if static_time_str is not None:
                    scheduled_arrival_ts = scheduled_ts_utc(service_date, static_time_str)
                    delay_seconds = int(
                        (observed_arrival_ts - scheduled_arrival_ts).total_seconds()
                    )
                else:
                    scheduled_arrival_ts = None
                    delay_seconds = None

                local_hour = observed_arrival_ts.astimezone(LOCAL_TZ).hour

                events.append(
                    {
                        "agency": "subway",
                        "route_id": trip["route_id"],
                        "direction": str(trip["direction_id"]),
                        "stop_id": row["stop_id"],
                        "vehicle_id": trip["vehicle_id"],
                        "trip_id": resolved_trip_id,
                        "observed_arrival_ts": observed_arrival_ts.isoformat(),
                        "scheduled_arrival_ts": (
                            scheduled_arrival_ts.isoformat() if scheduled_arrival_ts else None
                        ),
                        "delay_seconds": delay_seconds,
                        "predicted_arrival_ts_at_T_minus_5": None,
                        "service_date": service_date.isoformat(),
                        "day_type": day_type_for(service_date),
                        "hour_bucket": local_hour,
                        "derivation_quality": "clean",
                    }
                )

    if events:
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

    print(
        f"[ingest_subwaydata] {service_date} -> read={rows_read} written={len(events)} "
        f"skipped_trip_uid_not_found={skipped_trip_uid_not_found} "
        f"skipped_no_timestamp={skipped_no_timestamp} "
        f"skipped_unmatched_trip={skipped_unmatched_trip}"
    )


def run_ingest(
    raw_dir: Path,
    conn,
    trip_index: TripIndex,
    static_index: dict[tuple[str, str], str],
) -> None:
    for tar_path in sorted(raw_dir.glob("*.tar.xz")):
        service_date = date.fromisoformat(tar_path.name.removesuffix(".tar.xz"))
        process_day(conn, tar_path, service_date, trip_index, static_index)


if __name__ == "__main__":
    conn = get_connection()
    try:
        trip_index = TripIndex(SUBWAY_ZIP)
        static_index = build_static_stop_times_index(SUBWAY_ZIP)
        run_ingest(RAW_DIR, conn, trip_index, static_index)
    except Exception as exc:
        print(f"[ingest_subwaydata] fatal: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
