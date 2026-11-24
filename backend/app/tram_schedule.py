"""backend/app/tram_schedule.py

Deterministic, schedule-derived `typical_wait_seconds` for a tram leg
(2026-11-24, user) -- AirTrain JFK and the Roosevelt Island Tramway
(app/gtfs_paths.py's TRAM_ZIP_NAMES) have no GTFS-RT collection and never
will, so there's no reliability_buckets row for agency='tram' to read a
headway histogram from (same reasoning as ferry, which has none either).
Both run a real, fixed published timetable though, so "how long until
the next one" is directly computable from the static schedule itself --
same never-fabricate discipline as everywhere else in this product: only
the real timetable's own gaps, never a guessed/typical industry number.

Keyed by stop_id, not route_id, unlike app/bus_schedule_periods.py: both
tram feeds give every one of their routes an empty route_short_name
(confirmed against their real GTFS), which makes app/route_index.py's
RouteIndex.resolve() permanently ambiguous for them -- but there's no
need for a route_id here anyway, since a stop's own scheduled departures
already describe its real headway regardless of which route_id
technically generated them.

No period-clustering (contrast bus_schedule_periods.py) -- there's no
stored bucket to canonicalize across many history rows here, this is a
single live lookup per query, so it just answers the literal hour
directly or admits it can't (fewer than 2 real scheduled departures in
that hour), never borrows a neighboring hour's data.
"""
import csv
import io
import zipfile
from collections import defaultdict

from app.gtfs_paths import GTFS_DIR, TRAM_ZIP_NAMES

# Matches app/bus_schedule_periods.py's own weekday/weekend classification
# exactly (same real-world meaning -- "every real weekday", "either real
# weekend day" -- just reused here rather than re-derived).
_WEEKDAY_FIELDS = ("monday", "tuesday", "wednesday")

_headway_cache: dict[tuple[str, str], dict[int, float]] = {}


def _read_csv(z: zipfile.ZipFile, name: str) -> list[dict]:
    with z.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def _service_ids_for_day_type(calendar: list[dict], day_type: str) -> set[str]:
    if day_type == "weekday":
        return {r["service_id"] for r in calendar if all(r[field] == "1" for field in _WEEKDAY_FIELDS)}
    return {r["service_id"] for r in calendar if r["saturday"] == "1" or r["sunday"] == "1"}


def _scheduled_headway_by_hour(stop_id: str, day_type: str) -> dict[int, float]:
    """Real scheduled headway (seconds) by hour-of-day at this exact stop,
    for this day_type -- {} if the stop isn't found in either tram GTFS
    zip, or has fewer than 2 scheduled departures for this day_type."""
    for zip_name in TRAM_ZIP_NAMES:
        zip_path = GTFS_DIR / zip_name
        if not zip_path.exists():
            continue
        with zipfile.ZipFile(zip_path) as z:
            stop_times = _read_csv(z, "stop_times.txt")
            at_stop = [st for st in stop_times if st["stop_id"] == stop_id]
            if not at_stop:
                continue

            trips = _read_csv(z, "trips.txt")
            trip_to_service = {t["trip_id"]: t["service_id"] for t in trips}
            services = _service_ids_for_day_type(_read_csv(z, "calendar.txt"), day_type)

            times = []
            for st in at_stop:
                if trip_to_service.get(st["trip_id"]) not in services:
                    continue
                h, m, s = st["departure_time"].split(":")
                times.append(int(h) * 3600 + int(m) * 60 + int(s))
            times.sort()
            if len(times) < 2:
                return {}

            hour_gaps: dict[int, list[float]] = defaultdict(list)
            for a, b in zip(times, times[1:]):
                hour_gaps[(a // 3600) % 24].append(b - a)
            return {hour: sum(gaps) / len(gaps) for hour, gaps in hour_gaps.items()}
    return {}


def _get_headway_by_hour(stop_id: str, day_type: str) -> dict[int, float]:
    cache_key = (stop_id, day_type)
    if cache_key not in _headway_cache:
        _headway_cache[cache_key] = _scheduled_headway_by_hour(stop_id, day_type)
    return _headway_cache[cache_key]


def typical_wait_seconds(stop_id: str, day_type: str, hour: int) -> float | None:
    """Real scheduled headway (seconds) at this stop for this hour/
    day_type, or None if there's no real schedule evidence for it (stop
    not in either tram zip, or fewer than 2 departures to measure a real
    gap from) -- never a guess."""
    return _get_headway_by_hour(stop_id, day_type).get(hour)


def reset_cache_for_tests() -> None:
    _headway_cache.clear()
