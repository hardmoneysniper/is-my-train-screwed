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
GTFS_URLS = {
    "subway": "https://web.mta.info/developers/data/nyct/subway/google_transit.zip",
    "bus": "https://web.mta.info/developers/data/busco/google_transit.zip",
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
