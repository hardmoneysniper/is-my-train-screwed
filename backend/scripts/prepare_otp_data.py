import pathlib
import shutil

ROOT = pathlib.Path(__file__).parent.parent
OTP_CONFIG_DIR = ROOT / "otp_config"
OTP_DATA_DIR = ROOT / "data" / "otp"
GTFS_DIR = ROOT / "data" / "gtfs"

GTFS_FILES = {
    "subway.zip": GTFS_DIR / "subway.zip",
    "bus_busco.zip": GTFS_DIR / "bus.zip",
    "bus_manhattan.zip": GTFS_DIR / "bus_manhattan.zip",
    "bus_bronx.zip": GTFS_DIR / "bus_bronx.zip",
    "bus_brooklyn.zip": GTFS_DIR / "bus_brooklyn.zip",
    "bus_queens.zip": GTFS_DIR / "bus_queens.zip",
    "bus_staten_island.zip": GTFS_DIR / "bus_staten_island.zip",
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
