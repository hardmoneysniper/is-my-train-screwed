"""MTA GTFS-RT alerts feed client (subway + bus).

Fetches and parses the current-state alerts feeds -- no persistence, no
historical archival (unlike the bus reliability collector), since nothing
downstream needs historical alert stats. One AlertRecord per alert entity,
aggregating across all of that alert's informed_entity rows (route_ids and
stop_ids deduplicated) -- verified live this session that a single alert
commonly spans many informed_entity rows (avg ~9-10 for a stop-level alert)
and can span multiple routes, so a naive one-record-per-row model would
explode a single alert into near-duplicate records.

Fetch failures (network error, non-200, malformed protobuf) raise -- this
module doesn't catch/swallow. The caller (the Trip Monitor's poll loop, a
later task) is responsible for catching and skipping that cycle.
"""
import os
from datetime import datetime, timezone

import httpx
from google.transit import gtfs_realtime_pb2
from pydantic import BaseModel

SUBWAY_ALERTS_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"
BUS_ALERTS_URL = "https://gtfsrt.prod.obanyc.com/alerts"


class AlertRecord(BaseModel):
    alert_id: str
    route_ids: list[str]
    stop_ids: list[str]
    header_text: str
    active: bool


def _api_key() -> str:
    key = os.environ.get("MTA_BUSTIME_API_KEY")
    if not key:
        raise RuntimeError("MTA_BUSTIME_API_KEY not set")
    return key


def _plain_text_header(alert) -> str:
    """Return the plain-text (non-HTML) translation of header_text, matching
    the language code case-insensitively -- subway uses "en", bus uses "EN".
    Returns "" if no plain-text translation exists (never fabricate one)."""
    for translation in alert.header_text.translation:
        if translation.language.lower() == "en":
            return translation.text
    return ""


def _is_active(alert, now: datetime) -> bool:
    """GTFS-RT convention: no active_period entries at all means always-active."""
    if len(alert.active_period) == 0:
        return True
    now_ts = now.timestamp()
    for period in alert.active_period:
        start = period.start if period.HasField("start") else 0
        end = period.end if period.HasField("end") else 0
        if start and now_ts < start:
            continue
        if end and now_ts > end:
            continue
        return True
    return False


def _parse_alerts(feed: gtfs_realtime_pb2.FeedMessage) -> list[AlertRecord]:
    now = datetime.now(timezone.utc)
    records = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        if len(alert.informed_entity) == 0:
            continue

        header_text = _plain_text_header(alert)
        if not header_text:
            continue

        route_ids: list[str] = []
        stop_ids: list[str] = []
        for informed in alert.informed_entity:
            if informed.route_id and informed.route_id not in route_ids:
                route_ids.append(informed.route_id)
            if informed.stop_id and informed.stop_id not in stop_ids:
                stop_ids.append(informed.stop_id)

        records.append(AlertRecord(
            alert_id=entity.id,
            route_ids=route_ids,
            stop_ids=stop_ids,
            header_text=header_text,
            active=_is_active(alert, now),
        ))
    return records


async def fetch_subway_alerts() -> list[AlertRecord]:
    async with httpx.AsyncClient() as client:
        response = await client.get(SUBWAY_ALERTS_URL, timeout=15)
        response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return _parse_alerts(feed)


async def fetch_bus_alerts() -> list[AlertRecord]:
    key = _api_key()
    async with httpx.AsyncClient() as client:
        response = await client.get(BUS_ALERTS_URL, params={"key": key}, timeout=15)
        response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return _parse_alerts(feed)
