"""One-off utility: extract a bounding-box subregion from the NYC OSM PBF,
dropping Staten Island and the Bronx to shrink OTP's street-graph memory
footprint on Railway's 1GB/service cap (see Task 10's deployment notes).

pyosmium has no built-in bbox clipper (the maintainers point to the
separate `osmium-tool` C++ binary for that, which isn't pip-installable
and isn't available in this environment) -- doing it manually in Python
is fine for a one-off admin operation even though it's slower than the
native tool would be.

Two-pass approach, matching how real OSM extraction tools stay
structurally valid:
  Pass 1: find every way with at least one node inside the bbox; record
          that way's id AND every node id it references (not just the
          ones inside the box) -- a way missing even one of its nodes
          is a broken/dangling geometry OTP's street graph builder can't
          use.
  Pass 2: write out every node in the collected node-id set, then every
          way in the collected way-id set.

Relations are dropped entirely (not written to the output at all) -- OTP
uses relations mainly for turn restrictions on street routing; dropping
them is a minor routing-quality simplification (some turn restrictions
ignored), not a functional break, and avoids the much more involved
reference-completion logic multi-polygon/restriction relations would
otherwise need.

Bounding box is an approximation (a rectangle, not the real political
borough boundary), chosen to keep Manhattan/Brooklyn/Queens (the
beachhead + Q70/M60/Q102 service area) while excluding most of the Bronx
(north) and all of Staten Island (west, across the harbor):
  min_lon=-74.08 (Staten Island sits west of ~-74.05 across NY Harbor)
  max_lat=40.85  (the Bronx is mostly north of ~40.80-40.92; this also
                  clips the northernmost tip of Manhattan -- outside the
                  beachhead's service area, an accepted tradeoff)
Not verified against an authoritative borough-boundary dataset -- a
rectangle is a deliberate simplification, not a precise cut.
"""
import pathlib
import sys

import osmium

NYC_BBOX = (-74.08, 40.49, -73.70, 40.85)  # (min_lon, min_lat, max_lon, max_lat)


def _in_bbox(location, bbox) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= location.lon <= max_lon and min_lat <= location.lat <= max_lat


def trim_osm_extract(source_pbf: pathlib.Path, output_pbf: pathlib.Path, bbox=NYC_BBOX) -> None:
    keep_way_ids: set[int] = set()
    keep_node_ids: set[int] = set()

    for obj in osmium.FileProcessor(str(source_pbf)).with_locations():
        if obj.is_way():
            way_node_ids = [n.ref for n in obj.nodes if n.location.valid()]
            if any(_in_bbox(n.location, bbox) for n in obj.nodes if n.location.valid()):
                keep_way_ids.add(obj.id)
                keep_node_ids.update(way_node_ids)

    with osmium.SimpleWriter(str(output_pbf), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(source_pbf)):
            if obj.is_node() and obj.id in keep_node_ids:
                writer.add_node(obj)
            elif obj.is_way() and obj.id in keep_way_ids:
                writer.add_way(obj)


def main() -> None:
    root = pathlib.Path(__file__).parent.parent
    source = root / "data" / "otp" / "NewYork.osm.pbf"
    output = root / "data" / "otp" / "NewYork_trimmed.osm.pbf"

    if not source.exists():
        sys.exit(f"Missing source OSM file: {source}")

    trim_osm_extract(source, output)

    src_mb = source.stat().st_size / 1_000_000
    out_mb = output.stat().st_size / 1_000_000
    print(f"[trim_osm_extract] {source.name}: {src_mb:.1f}MB -> {output.name}: {out_mb:.1f}MB")


if __name__ == "__main__":
    main()
