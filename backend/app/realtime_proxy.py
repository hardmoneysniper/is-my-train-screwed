# backend/app/realtime_proxy.py
"""
Rewrites MTA subway GTFS-RT trip_ids to match the static schedule's trip_id
format before OTP consumes them.

Why this exists: MTA's real-time trip_id (e.g. "052250_6..N03R") is the
static trip_id with everything up to and including the first underscore
stripped off (the static form is "{schedule_prefix}_052250_6..N03R"). OTP's
built-in stop-time-updater does exact/fuzzy matching that can't bridge this
on its own -- verified live this session: 90% of real RT trip_ids exact-
suffix-match a real static trip_id, with correct direction/headsign/service
recovered from the match, so this is a real, simple format difference, not
a deeper data problem. This is a thin pass-through: fetch the real feed,
rewrite each trip_id to its matched static trip_id (leaving unmatched ones
as-is -- never guess), re-serve the protobuf. Point OTP's router-config.json
subway updaters at this instead of the raw MTA URLs.
"""
import csv
import datetime
import io
import pathlib
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from google.transit import gtfs_realtime_pb2

SUBWAY_ZIP = pathlib.Path(__file__).parent.parent / "data" / "gtfs" / "subway.zip"

FEED_GROUPS = {
    "ace": "nyct%2Fgtfs-ace",
    "bdfm": "nyct%2Fgtfs-bdfm",
    "g": "nyct%2Fgtfs-g",
    "jz": "nyct%2Fgtfs-jz",
    "nqrw": "nyct%2Fgtfs-nqrw",
    "l": "nyct%2Fgtfs-l",
    "numbered": "nyct%2Fgtfs",
    "si": "nyct%2Fgtfs-si",
}
MTA_BASE_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds"


class TripIndex:
    """Suffix-keyed lookup from RT-style trip_id to the matching static
    trip_id, with calendar-based disambiguation for suffixes shared by more
    than one static trip (e.g. a holiday-shifted schedule variant)."""

    def __init__(self, gtfs_zip_path: pathlib.Path):
        import zipfile

        self._by_suffix: dict[str, list[tuple[str, str]]] = {}  # suffix -> [(trip_id, service_id)]
        self._service_days: dict[str, dict] = {}  # service_id -> calendar.txt row
        self._service_exceptions: dict[tuple[str, str], str] = {}  # (service_id, date) -> exception_type

        with zipfile.ZipFile(gtfs_zip_path) as z:
            with z.open("trips.txt") as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                    trip_id = row["trip_id"]
                    if "_" not in trip_id:
                        continue
                    suffix = trip_id.split("_", 1)[1]
                    self._by_suffix.setdefault(suffix, []).append((trip_id, row["service_id"]))

            if "calendar.txt" in z.namelist():
                with z.open("calendar.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        self._service_days[row["service_id"]] = row

            if "calendar_dates.txt" in z.namelist():
                with z.open("calendar_dates.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        self._service_exceptions[(row["service_id"], row["date"])] = row["exception_type"]

    def _service_active(self, service_id: str, date_str: str) -> bool:
        exception = self._service_exceptions.get((service_id, date_str))
        if exception == "1":
            return True
        if exception == "2":
            return False
        cal = self._service_days.get(service_id)
        if cal is None:
            return False
        if not (cal["start_date"] <= date_str <= cal["end_date"]):
            return False
        date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        weekday_field = date.strftime("%A").lower()
        return cal.get(weekday_field) == "1"

    def resolve(self, rt_trip_id: str, start_date: str) -> str | None:
        """Return the matching static trip_id, or None if no confident match.
        Never guesses: an ambiguous multi-candidate case with no single
        active-service match returns None (left unrewritten) rather than
        picking arbitrarily."""
        candidates = self._by_suffix.get(rt_trip_id)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0]
        if not start_date:
            return None
        active = [tid for tid, service_id in candidates if self._service_active(service_id, start_date)]
        if len(active) == 1:
            return active[0]
        return None


_trip_index: TripIndex | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trip_index
    _trip_index = TripIndex(SUBWAY_ZIP)
    yield


app = FastAPI(title="Subway GTFS-RT trip-id rewriting proxy", lifespan=lifespan)


@app.get("/rt/{feed_group}")
async def rewritten_feed(feed_group: str):
    if feed_group not in FEED_GROUPS:
        raise HTTPException(404, f"Unknown feed group '{feed_group}'")

    url = f"{MTA_BASE_URL}/{FEED_GROUPS[feed_group]}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=15)
        response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    for entity in feed.entity:
        if entity.HasField("trip_update"):
            trip = entity.trip_update.trip
            matched = _trip_index.resolve(trip.trip_id, trip.start_date)
            if matched:
                trip.trip_id = matched

    return Response(content=feed.SerializeToString(), media_type="application/x-protobuf")
