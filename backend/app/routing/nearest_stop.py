import csv
import pathlib
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

    def nearest(self, lat: float, lon: float, k: int = 1) -> list[dict]:
        query_point_m = transform(_TO_METERS, Point(lon, lat))
        results = []
        for i in self._idx.nearest(query_point_m.bounds, k):
            stop = self._stops[i]
            distance_m = query_point_m.distance(self._points_m[i])
            results.append({**stop, "distance_m": distance_m})
        results.sort(key=lambda s: s["distance_m"])
        return results
