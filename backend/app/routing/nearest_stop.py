import csv
import io
import pathlib
import zipfile
from rtree import index as rtree_index
from shapely.geometry import Point
from shapely.ops import transform
import pyproj


# UTM zone 18N covers NYC and is equidistant (unlike Web Mercator/EPSG:3857,
# whose scale factor is sec(latitude) -- ~1.32x at NYC's ~40.7N, which was
# silently inflating every distance_m by ~32%). Ranking was unaffected since
# all NYC stops sit at similar latitude, but the reported distances were
# wrong. Not agency-agnostic beyond the NYC area; revisit if stops.txt ever
# covers a wider region than the MTA network.
_TO_METERS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform


class StopIndex:
    def __init__(self, stops: list[dict]):
        self._stops = stops
        self._idx = rtree_index.Index()
        self._points_m = []
        for i, stop in enumerate(stops):
            point_m = transform(_TO_METERS, Point(stop["lon"], stop["lat"]))
            self._points_m.append(point_m)
            self._idx.insert(i, point_m.bounds)

    @classmethod
    def from_gtfs(cls, stops_txt_path: pathlib.Path) -> "StopIndex":
        stops = []
        with open(stops_txt_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stops.append({
                    "stop_id": row["stop_id"],
                    "stop_name": row["stop_name"],
                    "lat": float(row["stop_lat"]),
                    "lon": float(row["stop_lon"]),
                })
        return cls(stops)

    @classmethod
    def from_gtfs_zips(cls, zip_paths: list[pathlib.Path]) -> "StopIndex":
        """Build a combined index from stops.txt inside each of several
        GTFS zip files (e.g. subway.zip + the 6 bus borough zips), merging
        all stops into one list. Mirrors realtime_proxy.py's TripIndex
        zip-reading pattern; parses rows the same way from_gtfs does."""
        stops = []
        for zip_path in zip_paths:
            with zipfile.ZipFile(zip_path) as z:
                with z.open("stops.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        stops.append({
                            "stop_id": row["stop_id"],
                            "stop_name": row["stop_name"],
                            "lat": float(row["stop_lat"]),
                            "lon": float(row["stop_lon"]),
                        })
        return cls(stops)

    def find_by_name(self, query: str, limit: int = 20) -> list[dict]:
        """Case-insensitive substring match of query against stop_name,
        sorted alphabetically by stop_name, capped at limit. No fuzzy
        matching or relevance ranking."""
        query_lower = query.lower()
        matches = [s for s in self._stops if query_lower in s["stop_name"].lower()]
        matches.sort(key=lambda s: s["stop_name"])
        return matches[:limit]

    def nearest(self, lat: float, lon: float, k: int = 1) -> list[dict]:
        query_point_m = transform(_TO_METERS, Point(lon, lat))
        results = []
        for i in self._idx.nearest(query_point_m.bounds, k):
            stop = self._stops[i]
            distance_m = query_point_m.distance(self._points_m[i])
            results.append({**stop, "distance_m": distance_m})
        results.sort(key=lambda s: s["distance_m"])
        return results


_GTFS_DIR = pathlib.Path(__file__).parent.parent.parent / "data" / "gtfs"
_GTFS_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
]

_stop_index: StopIndex | None = None


def get_stop_index() -> StopIndex:
    """Lazily builds and caches the combined 7-feed StopIndex at module
    level. ConversationAgent is instantiated fresh per HTTP request (Task
    8), so this must NOT be rebuilt per-request -- built once on first call,
    reused on every call after."""
    global _stop_index
    if _stop_index is None:
        zip_paths = [_GTFS_DIR / name for name in _GTFS_ZIP_NAMES]
        _stop_index = StopIndex.from_gtfs_zips(zip_paths)
    return _stop_index
