"""Filter a GTFS zip down to a set of route_ids, preserving referential integrity.

Follows GTFS's reference graph (routes -> trips -> stop_times -> stops,
trips -> calendar/calendar_dates, trips -> shapes) so the output is a valid,
drop-in-replacement GTFS feed containing only the requested routes and
everything they reference. Built for Task 10b: shrinking the full 6-borough
bus GTFS down to the 3 corridors this project actually collects data for
(Q70+, M60+, Q102), to fix an OTP graph-build OOM on Railway's 1GB RAM cap.

Usage:
    python scripts/filter_gtfs_by_route.py <source_zip> <output_zip> --route-ids Q70+ M60+
"""

import argparse
import csv
import io
import zipfile
from pathlib import Path

# Core files that participate in the reference-graph walk. Every other file
# present in the source zip (agency.txt, feed_info.txt, transfers.txt, ...)
# is copied through unfiltered per the brief -- small, harmless, and safer
# than guessing whether they need filtering.
_FILTERED_FILES = {
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "stops.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "shapes.txt",
}

_REQUIRED_FILES = {"routes.txt", "trips.txt", "stop_times.txt", "stops.txt"}


def _read_csv_rows(zf: zipfile.ZipFile, filename: str):
    """Return (fieldnames, rows) for a GTFS text file, or (None, None) if absent."""
    if filename not in zf.namelist():
        return None, None
    with zf.open(filename) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _write_csv(zf: zipfile.ZipFile, filename: str, fieldnames, rows) -> None:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(filename, buf.getvalue())


def filter_gtfs_by_route(source_zip: Path, output_zip: Path, route_ids) -> dict:
    """Filter source_zip down to route_ids, writing the result to output_zip.

    Returns a dict of {filename: kept_row_count} for reporting/sanity-checking.
    """
    route_ids = set(route_ids)

    with zipfile.ZipFile(source_zip, "r") as src:
        namelist = set(src.namelist())
        for required in _REQUIRED_FILES:
            if required not in namelist:
                raise ValueError(f"source GTFS zip is missing required file: {required}")

        routes_fields, routes_rows = _read_csv_rows(src, "routes.txt")
        kept_routes = [r for r in routes_rows if r.get("route_id") in route_ids]

        trips_fields, trips_rows = _read_csv_rows(src, "trips.txt")
        kept_trips = [t for t in trips_rows if t.get("route_id") in route_ids]
        kept_trip_ids = {t["trip_id"] for t in kept_trips if t.get("trip_id")}
        kept_service_ids = {t["service_id"] for t in kept_trips if t.get("service_id")}

        stop_times_fields, stop_times_rows = _read_csv_rows(src, "stop_times.txt")
        kept_stop_times = [st for st in stop_times_rows if st.get("trip_id") in kept_trip_ids]
        kept_stop_ids = {st["stop_id"] for st in kept_stop_times if st.get("stop_id")}

        stops_fields, stops_rows = _read_csv_rows(src, "stops.txt")
        kept_stop_ids_with_parents = set(kept_stop_ids)
        if stops_fields and "parent_station" in stops_fields:
            stops_by_id = {s["stop_id"]: s for s in stops_rows if s.get("stop_id")}
            for stop_id in kept_stop_ids:
                stop_row = stops_by_id.get(stop_id)
                if stop_row is None:
                    continue
                parent = stop_row.get("parent_station")
                if parent:
                    kept_stop_ids_with_parents.add(parent)
        kept_stops = [s for s in stops_rows if s.get("stop_id") in kept_stop_ids_with_parents]

        calendar_fields, calendar_rows = _read_csv_rows(src, "calendar.txt")
        if calendar_rows is not None:
            calendar_rows = [c for c in calendar_rows if c.get("service_id") in kept_service_ids]

        calendar_dates_fields, calendar_dates_rows = _read_csv_rows(src, "calendar_dates.txt")
        if calendar_dates_rows is not None:
            calendar_dates_rows = [
                c for c in calendar_dates_rows if c.get("service_id") in kept_service_ids
            ]

        shapes_fields, shapes_rows = _read_csv_rows(src, "shapes.txt")
        if shapes_rows is not None:
            kept_shape_ids = set()
            if trips_fields and "shape_id" in trips_fields:
                kept_shape_ids = {t["shape_id"] for t in kept_trips if t.get("shape_id")}
            shapes_rows = [s for s in shapes_rows if s.get("shape_id") in kept_shape_ids]

        output_zip.parent.mkdir(parents=True, exist_ok=True)
        counts = {}
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as dst:
            _write_csv(dst, "routes.txt", routes_fields, kept_routes)
            counts["routes.txt"] = len(kept_routes)

            _write_csv(dst, "trips.txt", trips_fields, kept_trips)
            counts["trips.txt"] = len(kept_trips)

            _write_csv(dst, "stop_times.txt", stop_times_fields, kept_stop_times)
            counts["stop_times.txt"] = len(kept_stop_times)

            _write_csv(dst, "stops.txt", stops_fields, kept_stops)
            counts["stops.txt"] = len(kept_stops)

            if calendar_rows is not None:
                _write_csv(dst, "calendar.txt", calendar_fields, calendar_rows)
                counts["calendar.txt"] = len(calendar_rows)

            if calendar_dates_rows is not None:
                _write_csv(dst, "calendar_dates.txt", calendar_dates_fields, calendar_dates_rows)
                counts["calendar_dates.txt"] = len(calendar_dates_rows)

            if shapes_rows is not None:
                _write_csv(dst, "shapes.txt", shapes_fields, shapes_rows)
                counts["shapes.txt"] = len(shapes_rows)

            # Everything else (agency.txt, feed_info.txt, transfers.txt, ...)
            # is copied through byte-for-byte, unfiltered.
            for name in sorted(namelist - _FILTERED_FILES):
                dst.writestr(name, src.read(name))
                counts[name] = None

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter a GTFS zip down to a set of route_ids, preserving referential integrity."
    )
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("output_zip", type=Path)
    parser.add_argument("--route-ids", nargs="+", required=True, metavar="ROUTE_ID")
    args = parser.parse_args()

    counts = filter_gtfs_by_route(args.source_zip, args.output_zip, args.route_ids)
    print(f"[filter_gtfs_by_route] {args.source_zip} -> {args.output_zip} (routes: {', '.join(args.route_ids)})")
    for filename, count in counts.items():
        if count is None:
            print(f"[filter_gtfs_by_route]   {filename}: copied through unfiltered")
        else:
            print(f"[filter_gtfs_by_route]   {filename}: {count} rows kept")


if __name__ == "__main__":
    main()
