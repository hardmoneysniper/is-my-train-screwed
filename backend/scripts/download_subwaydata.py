import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

# subwaydata.nyc publishes one archive per calendar day, containing exactly
# two CSVs (trips + stop_times) named with the date embedded. Confirmed live
# 2026-08-28 (see task-2-brief.md): 302-redirects to a hashed CDN URL, handled
# transparently by follow_redirects=True same as load_static_gtfs.py.
#
# A day's data is published with a lag (the current day, and sometimes the
# tail end of the previous day, legitimately 404 until the next morning) --
# that is expected, not an error, and must be skipped rather than aborting
# the whole backfill. Any other unexpected status is a real failure.
URL_TEMPLATE = "https://subwaydata.nyc/data/subwaydatanyc_{date}_csv.tar.xz"

BACKFILL_DAYS = 90


def daterange(end_date: date, days: int) -> list[date]:
    return [end_date - timedelta(days=offset) for offset in range(days)]


def check_day_available(url: str) -> bool | None:
    """Returns True if published, False if not-yet-published (404), and
    raises for any other unexpected status (5xx, timeout, etc.)."""
    response = httpx.head(url, follow_redirects=True, timeout=15)
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        response.raise_for_status()
    return True


def download_subwaydata_day(url: str, dest: pathlib.Path) -> pathlib.Path:
    """Streams to a .tmp sibling and atomically renames into place only after
    a full, successful download -- so a mid-download interruption (network
    drop, Ctrl+C, laptop sleep) can never leave a truncated file at `dest`
    for the idempotency check (`dest.exists()`) to mistake for complete and
    silently skip forever. Matches bus_collector.py's convention of never
    letting a partially-written file be mistaken for a finished one."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with open(tmp_dest, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
        os.replace(tmp_dest, dest)
    except BaseException:
        tmp_dest.unlink(missing_ok=True)
        raise
    return dest


def run_backfill(data_dir: pathlib.Path, end_date: date, days: int = BACKFILL_DAYS) -> None:
    for day in daterange(end_date, days):
        day_str = day.isoformat()
        dest = data_dir / f"{day_str}.tar.xz"

        if dest.exists():
            print(f"[download_subwaydata] {day_str} -> skipped (already downloaded)")
            continue

        url = URL_TEMPLATE.format(date=day_str)
        available = check_day_available(url)
        if not available:
            print(f"[download_subwaydata] {day_str} -> skipped (not yet published)")
            continue

        download_subwaydata_day(url, dest)
        print(f"[download_subwaydata] {day_str} -> {dest}")


if __name__ == "__main__":
    data_dir = pathlib.Path(__file__).parent.parent / "data" / "raw" / "subway"
    today = datetime.now(timezone.utc).date()
    try:
        run_backfill(data_dir, today)
    except httpx.HTTPError as exc:
        print(f"[download_subwaydata] fatal: {exc}", file=sys.stderr)
        sys.exit(1)
