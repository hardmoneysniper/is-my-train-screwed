import pathlib
import shutil

from scripts.filter_gtfs_by_route import filter_gtfs_by_route

ROOT = pathlib.Path(__file__).parent.parent
OTP_CONFIG_DIR = ROOT / "otp_config"
OTP_DATA_DIR = ROOT / "data" / "otp"
GTFS_DIR = ROOT / "data" / "gtfs"

# Bus GTFS is filtered down to just the 3 collected corridors (Q70+, M60+,
# Q102) -- Task 10's real deployment attempt found the full 6-borough bus
# GTFS makes OTP's graph too large for Railway's 1GB/service RAM cap (OOM,
# reproduced even with an explicit heap cap). This product only ever
# collects reliability data for these 3 routes anyway (see
# backend/collectors/bus_collector.py's CORRIDORS), so routing capability
# for the other ~300+ MTA bus routes was never actually used. M60+ has no
# real trips outside bus_manhattan.zip (checked directly against real
# data, not assumed) -- the other 4 borough files are dropped entirely,
# not just filtered, since they contribute nothing.
GTFS_FILES = {
    "subway.zip": GTFS_DIR / "subway.zip",
}
FILTERED_BUS_FILES = {
    # output filename: (source zip, [route_ids to keep])
    "bus_filtered.zip": (GTFS_DIR / "bus.zip", ["Q70+", "Q102"]),
    "bus_manhattan_filtered.zip": (GTFS_DIR / "bus_manhattan.zip", ["M60+"]),
}


def main():
    OTP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    missing = [name for name, src in GTFS_FILES.items() if not src.exists()]
    if missing:
        raise SystemExit(
            f"Missing static GTFS source file(s): {missing}. "
            f"Run `python scripts/load_static_gtfs.py` first."
        )
    for name, src in GTFS_FILES.items():
        shutil.copyfile(src, OTP_DATA_DIR / name)

    for out_name, (src, route_ids) in FILTERED_BUS_FILES.items():
        if not src.exists():
            raise SystemExit(
                f"Missing static GTFS source file: {src}. "
                f"Run `python scripts/load_static_gtfs.py` first."
            )
        filter_gtfs_by_route(src, OTP_DATA_DIR / out_name, route_ids)

    osm_dest = OTP_DATA_DIR / "NewYork.osm.pbf"
    if not osm_dest.exists():
        raise SystemExit(
            f"Missing {osm_dest} -- download it from "
            "https://download.bbbike.org/osm/bbbike/NewYork/NewYork.osm.pbf "
            "(~150MB) before running this script."
        )

    shutil.copyfile(OTP_CONFIG_DIR / "build-config.json", OTP_DATA_DIR / "build-config.json")
    # router-config.json's ${MTA_BUSTIME_API_KEY} is OTP's own env-var
    # interpolation syntax, substituted by OTP itself at startup -- copied
    # verbatim, no rendering needed here. docker-compose.yml passes the
    # real value into the container's environment (auto-loaded from
    # backend/.env by Compose's own interpolation).
    shutil.copyfile(OTP_CONFIG_DIR / "router-config.json", OTP_DATA_DIR / "router-config.json")

    print(f"[prepare_otp_data] OTP data directory ready at {OTP_DATA_DIR}")


if __name__ == "__main__":
    main()
