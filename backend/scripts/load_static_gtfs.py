import pathlib
import sys
import httpx

# Candidate URLs — subway URL is pattern-matched from mini-nyc-3d's confirmed-live
# LIRR/MNR download URLs (web.mta.info/developers/data/{agency}/google_transit.zip);
# NOT independently confirmed against the MTA developer portal. See REUSE.md §4.
#
# Bus URL: the pattern-matched candidate ("nyct/bus") 404'd during Task 2's
# live verification. "busco" was found by probing variations and confirmed
# to return a real, complete MTA Bus Company GTFS feed (checked by hand:
# real Queens routes under agency_id MTABC, all 9 standard GTFS files
# present, correct sizes) -- but like subway, it is an HTTP-level check,
# not a cross-reference against MTA's documented developer portal listing.
# Same confidence caveat as subway above.
#
# The 5 "bus_*" borough entries below were added in Task 10 after discovering
# that "bus" above (agency busco / MTA Bus Company) is only 92 routes, mostly
# Queens -- it does NOT include "MTA New York City Transit" bus routes, which
# is most of the citywide network, including M60 (one of the spec's two named
# seed bus corridors). These 5 NYCT bus feeds are split by borough and were
# confirmed live this session (HTTP 200, agency MTA New York City Transit,
# 307 routes each, manhattan.zip confirmed to contain route M60).
GTFS_URLS = {
    "subway": "https://web.mta.info/developers/data/nyct/subway/google_transit.zip",
    "bus": "https://web.mta.info/developers/data/busco/google_transit.zip",
    "bus_manhattan": "https://web.mta.info/developers/data/nyct/bus/google_transit_manhattan.zip",
    "bus_bronx": "https://web.mta.info/developers/data/nyct/bus/google_transit_bronx.zip",
    "bus_brooklyn": "https://web.mta.info/developers/data/nyct/bus/google_transit_brooklyn.zip",
    "bus_queens": "https://web.mta.info/developers/data/nyct/bus/google_transit_queens.zip",
    "bus_staten_island": "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
}


def verify_gtfs_url(url: str) -> bool:
    response = httpx.head(url, follow_redirects=True, timeout=15)
    content_type = response.headers.get("content-type", "")
    return response.status_code == 200 and "zip" in content_type


def download_gtfs(url: str, dest: pathlib.Path) -> pathlib.Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return dest


if __name__ == "__main__":
    data_dir = pathlib.Path(__file__).parent.parent / "data" / "gtfs"
    for name, url in GTFS_URLS.items():
        if not verify_gtfs_url(url):
            print(f"[load_static_gtfs] {name} URL is not live: {url}", file=sys.stderr)
            sys.exit(1)
        dest = data_dir / f"{name}.zip"
        download_gtfs(url, dest)
        print(f"[load_static_gtfs] {name} -> {dest}")
