"""backend/app/gtfs_paths.py

Single source of truth for where the static GTFS zips live, pulled out
of risk_engine.py and routing/nearest_stop.py (2026-11-06 cleanup),
which each independently defined an identical GTFS_DIR + 8-entry zip
list -- real duplication, not just similar-looking code.

BUS_ZIP_NAMES keeps its exact original order from bus_schedule_periods.py
rather than being derived from ALL_ZIP_NAMES (whose order differs):
_scheduled_headway_by_hour there walks this list and returns on the
first zip with real trip data for a given route, so reordering it could
silently change which zip "wins" for a route with data in more than
one. ALL_ZIP_NAMES is independently safe to reorder (RouteIndex.resolve
and StopIndex.find_by_name/nearest are both order-independent -- set-
membership and explicit sorts, never "first match wins"), but is kept
as its own literal list here anyway rather than built by concatenation,
so nothing about this consolidation can change either module's
behavior.
"""
import pathlib

GTFS_DIR = pathlib.Path(__file__).parent.parent / "data" / "gtfs"

ALL_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
    "ferry.zip",
]

BUS_ZIP_NAMES = [
    "bus.zip", "bus_bronx.zip", "bus_brooklyn.zip",
    "bus_manhattan.zip", "bus_queens.zip", "bus_staten_island.zip",
]
