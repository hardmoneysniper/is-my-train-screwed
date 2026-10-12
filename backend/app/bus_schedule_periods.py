"""backend/app/bus_schedule_periods.py

Collapses bus reliability_buckets' hour-of-day granularity into real,
per-route SCHEDULED-headway periods (2026-10-12, user) -- real data
showed no universal rush/midday/night split works across routes (e.g.
Q90 is flat ~20min headway nearly all day, M60+ tapers gradually, Q102
is choppy and inverted from typical rush-hour expectations), so periods
are derived per-route from each route's real static GTFS schedule, not
guessed or hardcoded.

Algorithm: for a (route_id, day_type), compute real scheduled headway
at that route's busiest stop for each hour that has scheduled service
(from trips.txt + stop_times.txt + calendar.txt -- MTA's bus GTFS has
no frequencies.txt, so this is derived directly from explicit trip
schedules, not read off a ready-made field). Walk hours in order
starting at ANCHOR_HOUR (a real, defensible overnight-service-transition
point, not a universal rush/midday boundary), merging each hour into the
current period if its headway is within PERIOD_MERGE_TOLERANCE of the
PREVIOUS hour's (not a fixed anchor value, so gradual drift across many
hours doesn't silently create one giant period) -- otherwise starting a
new period. An hour with zero scheduled service is left unmapped (no
evidence to justify merging it with anything); period_hour_for falls
back to returning that hour unchanged.

Costly to compute (parses the real GTFS zips), so cached per
(route_id, day_type) the same way risk_engine.py caches its RouteIndex.
"""
import csv
import io
import pathlib
import zipfile
from collections import defaultdict

GTFS_DIR = pathlib.Path(__file__).parent.parent / "data" / "gtfs"
BUS_ZIP_NAMES = [
    "bus.zip", "bus_bronx.zip", "bus_brooklyn.zip",
    "bus_manhattan.zip", "bus_queens.zip", "bus_staten_island.zip",
]

ANCHOR_HOUR = 5  # a real, common overnight-to-early-morning service
# transition point for NYC bus schedules -- not a universal rush/midday
# boundary, just where the hour-walk starts so a genuine night period
# isn't artificially split by the 0-23 wraparound.
PERIOD_MERGE_TOLERANCE = 0.3  # 30% relative headway change vs. the
# previous hour in the walk triggers a new period; policy choice, not
# derived -- coarse enough to collapse near-identical adjacent hours,
# fine enough to not flatten a route's whole day into one period.

_period_cache: dict[tuple[str, str], dict[int, int]] = {}


def _read_csv(z: zipfile.ZipFile, name: str) -> list[dict]:
    with z.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def _scheduled_headway_by_hour(route_id: str, day_type: str) -> dict[int, float]:
    """Real scheduled headway (minutes) by hour-of-day, at this route's
    busiest stop, for the given day_type. {} if the route isn't found in
    any bus GTFS zip, or has fewer than 2 scheduled departures."""
    for zip_name in BUS_ZIP_NAMES:
        zip_path = GTFS_DIR / zip_name
        if not zip_path.exists():
            continue
        with zipfile.ZipFile(zip_path) as z:
            routes = _read_csv(z, "routes.txt")
            if not any(r["route_id"] == route_id for r in routes):
                continue

            calendar = _read_csv(z, "calendar.txt")
            if day_type == "weekday":
                services = {
                    r["service_id"] for r in calendar
                    if r["monday"] == "1" and r["tuesday"] == "1" and r["wednesday"] == "1"
                }
            else:
                services = {
                    r["service_id"] for r in calendar
                    if r["saturday"] == "1" or r["sunday"] == "1"
                }

            # 2026-10-12 bug fix: routes.txt listing this route_id doesn't
            # mean THIS zip has real trips for it -- CLAUDE.md's own
            # documented gotcha (e.g. M60+ has real trips only in
            # bus_manhattan.zip, appears route-id-only with zero trips in
            # the other 5 borough files). Every "no real data here" case
            # below must `continue` to the next zip, never `return {}`
            # early -- confirmed live: without this fix, Q90/M60+ (both
            # multi-zip-listed routes) silently resolved to zero periods.
            trips = [t for t in _read_csv(z, "trips.txt") if t["route_id"] == route_id]
            trip_to_service = {t["trip_id"]: t["service_id"] for t in trips}
            trip_ids = set(trip_to_service)
            if not trip_ids:
                continue

            stop_times = [st for st in _read_csv(z, "stop_times.txt") if st["trip_id"] in trip_ids]
            stop_counts: dict[str, int] = defaultdict(int)
            for st in stop_times:
                stop_counts[st["stop_id"]] += 1
            if not stop_counts:
                continue
            rep_stop = max(stop_counts, key=stop_counts.get)

            times = []
            for st in stop_times:
                if st["stop_id"] != rep_stop or trip_to_service.get(st["trip_id"]) not in services:
                    continue
                h, m, s = st["departure_time"].split(":")
                times.append(int(h) * 3600 + int(m) * 60 + int(s))
            times.sort()
            if len(times) < 2:
                continue

            hour_gaps: dict[int, list[float]] = defaultdict(list)
            for a, b in zip(times, times[1:]):
                hour = (a // 3600) % 24
                hour_gaps[hour].append((b - a) / 60.0)
            return {hour: sum(gaps) / len(gaps) for hour, gaps in hour_gaps.items()}
    return {}


def _cluster_into_periods(hour_headway: dict[int, float]) -> dict[int, int]:
    """Hours with real scheduled headway data only -- walks them in order
    starting at ANCHOR_HOUR, wrapping through 0-23, merging an hour into
    the current period if its headway is within PERIOD_MERGE_TOLERANCE of
    the PREVIOUS hour walked (not a fixed period-start value, so gradual
    drift doesn't silently collapse a whole day into one period)."""
    walk_order = [(ANCHOR_HOUR + i) % 24 for i in range(24)]
    known_hours = [h for h in walk_order if h in hour_headway]
    if not known_hours:
        return {}

    mapping: dict[int, int] = {}
    current_period_hour = known_hours[0]
    prev_headway = hour_headway[known_hours[0]]
    mapping[known_hours[0]] = current_period_hour

    for hour in known_hours[1:]:
        headway = hour_headway[hour]
        relative_change = abs(headway - prev_headway) / prev_headway if prev_headway else 0.0
        if relative_change > PERIOD_MERGE_TOLERANCE:
            current_period_hour = hour
        mapping[hour] = current_period_hour
        prev_headway = headway

    return mapping


def period_hour_for(route_id: str, day_type: str, hour: int) -> int:
    """Canonical period-hour for this real hour, given this route's real
    scheduled headway pattern -- use this in place of the literal hour
    for BUS bucket keys (both writing and querying reliability_buckets),
    never for subway (which keeps literal hour_bucket unchanged, it
    already has sufficient per-hour coverage). Falls back to the literal
    hour unchanged when the route/hour has no scheduled-service evidence
    to justify a merge -- never guesses a grouping it can't support."""
    cache_key = (route_id, day_type)
    if cache_key not in _period_cache:
        headway_by_hour = _scheduled_headway_by_hour(route_id, day_type)
        _period_cache[cache_key] = _cluster_into_periods(headway_by_hour)
    return _period_cache[cache_key].get(hour, hour)


def reset_cache_for_tests() -> None:
    _period_cache.clear()
