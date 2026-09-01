import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from app.alerts import fetch_bus_alerts, fetch_subway_alerts


def _feed_message(entities: list) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = int(time.time())
    for entity in entities:
        feed.entity.append(entity)
    return feed


def _multi_route_multi_stop_alert(alert_id="alert-1", language="en") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="A and D trains delayed", language=language)
    alert.header_text.translation.add(text="<p>A and D trains delayed</p>", language=f"{language}-html")
    # Same route repeated across multiple stops (dedup within a route)...
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="A01")
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="A02")
    # ...and a second route sharing the same header (multi-route alert).
    alert.informed_entity.add(agency_id="MTASBWY", route_id="D", stop_id="D01")
    return entity


def _route_only_alert(alert_id="alert-2") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="Systemwide delays", language="en")
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="")
    return entity


def _uppercase_language_alert(alert_id="alert-3") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="Bus detour on Q70+", language="EN")
    alert.informed_entity.add(agency_id="MTABC", route_id="Q70+", stop_id="")
    return entity


def _no_active_period_alert(alert_id="alert-4") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="No active_period at all", language="en")
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="")
    return entity


def _future_start_alert(alert_id="alert-5") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="Starts in the future", language="en")
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="")
    period = alert.active_period.add()
    period.start = int(time.time()) + 3600
    return entity


def _ongoing_alert(alert_id="alert-6") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="Started in the past, no end", language="en")
    alert.informed_entity.add(agency_id="MTASBWY", route_id="A", stop_id="")
    period = alert.active_period.add()
    period.start = int(time.time()) - 3600
    return entity


def _no_informed_entity_alert(alert_id="alert-7") -> gtfs_realtime_pb2.FeedEntity:
    entity = gtfs_realtime_pb2.FeedEntity()
    entity.id = alert_id
    alert = entity.alert
    alert.header_text.translation.add(text="Orphan alert with no informed_entity rows", language="en")
    return entity


def _mock_response(content: bytes) -> MagicMock:
    response = MagicMock()
    response.content = content
    response.raise_for_status = lambda: None
    return response


@pytest.mark.asyncio
async def test_multi_route_multi_stop_alert_aggregates_into_one_record():
    feed = _feed_message([_multi_route_multi_stop_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert len(records) == 1
    record = records[0]
    assert record.alert_id == "alert-1"
    assert sorted(record.route_ids) == ["A", "D"]
    assert sorted(record.stop_ids) == ["A01", "A02", "D01"]
    assert record.header_text == "A and D trains delayed"


@pytest.mark.asyncio
async def test_route_only_alert_has_empty_stop_ids():
    feed = _feed_message([_route_only_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert len(records) == 1
    assert records[0].stop_ids == []
    assert records[0].route_ids == ["A"]


@pytest.mark.asyncio
async def test_uppercase_language_code_matched_case_insensitively():
    feed = _feed_message([_uppercase_language_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ), patch.dict("os.environ", {"MTA_BUSTIME_API_KEY": "test-key"}):
        records = await fetch_bus_alerts()

    assert len(records) == 1
    assert records[0].header_text == "Bus detour on Q70+"


@pytest.mark.asyncio
async def test_no_active_period_defaults_to_active_true():
    feed = _feed_message([_no_active_period_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert records[0].active is True


@pytest.mark.asyncio
async def test_future_start_period_is_not_active():
    feed = _feed_message([_future_start_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert records[0].active is False


@pytest.mark.asyncio
async def test_past_start_no_end_is_active_ongoing():
    feed = _feed_message([_ongoing_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert records[0].active is True


@pytest.mark.asyncio
async def test_alert_with_zero_informed_entity_rows_is_skipped():
    feed = _feed_message([_no_informed_entity_alert(), _multi_route_multi_stop_alert()])
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_mock_response(feed.SerializeToString()),
    ):
        records = await fetch_subway_alerts()

    assert len(records) == 1
    assert records[0].alert_id == "alert-1"


@pytest.mark.asyncio
async def test_http_failure_propagates():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=MagicMock()
    ))
    with patch(
        "app.alerts.httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_subway_alerts()
