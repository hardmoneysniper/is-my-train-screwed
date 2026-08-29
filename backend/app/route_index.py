"""backend/app/route_index.py

Maps a GTFS `route_short_name` (what OTP's `route.shortName` gives a
`Leg`) to the `route_id` actually stored in `arrival_events` /
`reliability_buckets` (Tasks 3/4's schema) -- see task-6-brief.md Gap 2.

For 25 of 29 subway routes these are identical, but real routes we care
about differ (e.g. the "S"-branded subway shuttles are GS/FS/H in
route_id; Q70+/M60+ are "Q70-SBS"/"M60-SBS" in route_short_name). Built
from real GTFS `routes.txt` files, following `StopIndex.from_gtfs`'s
convention in `backend/app/routing/nearest_stop.py` -- never hardcoded
from this brief's own example table.

A short_name matched by more than one route_id (the three subway
shuttles all short-named "S" is a genuine, real ambiguity, not a typo)
is unmatchable: `resolve()` returns None rather than guessing which one
a leg means. Callers must treat None as "insufficient", never pick
arbitrarily.
"""
import csv
import io
import pathlib
import zipfile
from typing import Iterable


class RouteIndex:
    def __init__(self, routes: list[dict]):
        self._by_short_name: dict[str, set[str]] = {}
        for route in routes:
            short_name = route["route_short_name"]
            self._by_short_name.setdefault(short_name, set()).add(route["route_id"])

    @classmethod
    def from_gtfs(cls, gtfs_zip_paths: Iterable[pathlib.Path]) -> "RouteIndex":
        """Build from one or more static GTFS zips (subway.zip + the 6 bus
        zips -- see task-6-brief.md Gap 2), reading each one's `routes.txt`
        directly out of the zip. Matches the zip-reading convention already
        established in this codebase (`ingest_subwaydata.py`,
        `realtime_proxy.py`'s `TripIndex`) rather than requiring pre-extracted
        CSVs."""
        routes = []
        for path in gtfs_zip_paths:
            with zipfile.ZipFile(path) as z:
                with z.open("routes.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        routes.append(
                            {
                                "route_id": row["route_id"],
                                "route_short_name": row["route_short_name"],
                            }
                        )
        return cls(routes)

    def resolve(self, route_short_name: str | None) -> str | None:
        """Return the unique route_id for this short_name, or None if the
        short_name is unknown OR ambiguous (matched by >1 route_id) --
        never guesses between candidates."""
        if route_short_name is None:
            return None
        candidates = self._by_short_name.get(route_short_name)
        if not candidates or len(candidates) != 1:
            return None
        return next(iter(candidates))
