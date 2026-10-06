import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from app.routing.otp_client import OTPClient
from app.models.transit import Itinerary

MOCK_OTP_RESPONSE = {
    "data": {
        "plan": {
            "itineraries": [
                {
                    "duration": 1800,
                    "legs": [
                        {
                            "mode": "SUBWAY",
                            "route": {"shortName": "F"},
                            "from": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06S"}},
                            "to": {"name": "Lexington Av/63 St", "stop": {"gtfsId": "MTA_NYCT_Subway:B08S"}},
                            "startTime": 1755100800000,
                            "endTime": 1755101400000,
                            "realTime": True,
                            "arrivalDelay": 90,
                        }
                    ],
                }
            ]
        }
    }
}

@pytest.mark.asyncio
async def test_plan_route_parses_itineraries():
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_OTP_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        itineraries = await client.plan_route(40.7597, -73.9532, 40.7644, -73.9656)

    mock_post.assert_awaited_once()
    call_kwargs = mock_post.await_args.kwargs
    assert call_kwargs["json"]["variables"] == {
        "fromLat": 40.7597,
        "fromLon": -73.9532,
        "toLat": 40.7644,
        "toLon": -73.9656,
    }
    assert "plan(" in call_kwargs["json"]["query"]

    assert len(itineraries) == 1
    assert itineraries[0].duration_seconds == 1800
    assert itineraries[0].legs[0].route_short_name == "F"
    assert itineraries[0].legs[0].from_stop_id == "MTA_NYCT_Subway:B06S"
    assert itineraries[0].legs[0].to_stop_name == "Lexington Av/63 St"
    assert itineraries[0].legs[0].start_time_ms == 1755100800000
    assert itineraries[0].legs[0].real_time is True
    assert itineraries[0].legs[0].arrival_delay_seconds == 90


@pytest.mark.asyncio
async def test_plan_route_defaults_real_time_fields_when_absent():
    """OTP responses that omit realTime/arrivalDelay (e.g. no RT match) must not break parsing."""
    response_without_rt_fields = {
        "data": {
            "plan": {
                "itineraries": [
                    {
                        "duration": 1800,
                        "legs": [
                            {
                                "mode": "SUBWAY",
                                "route": {"shortName": "F"},
                                "from": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06S"}},
                                "to": {"name": "Lexington Av/63 St", "stop": {"gtfsId": "MTA_NYCT_Subway:B08S"}},
                                "startTime": 1755100800000,
                                "endTime": 1755101400000,
                            }
                        ],
                    }
                ]
            }
        }
    }
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = response_without_rt_fields
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        itineraries = await client.plan_route(40.7597, -73.9532, 40.7644, -73.9656)

    assert itineraries[0].legs[0].real_time is False
    assert itineraries[0].legs[0].arrival_delay_seconds is None


MOCK_OTP_RESPONSE_WITH_WALK_DATA = {
    "data": {
        "plan": {
            "itineraries": [
                {
                    "duration": 1800,
                    "legs": [
                        {
                            "mode": "WALK",
                            "route": None,
                            "from": {"name": "2 West Loop Rd", "stop": None, "lat": 40.7566, "lon": -73.9557},
                            "to": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06N"}, "lat": 40.7597, "lon": -73.9532},
                            "startTime": 1755100000000,
                            "endTime": 1755100500000,
                            "headsign": None,
                            "steps": [
                                {
                                    "streetName": "Main St",
                                    "distance": 120.5,
                                    "relativeDirection": "LEFT",
                                    "absoluteDirection": "NORTH",
                                    "exit": None,
                                    "stayOn": False,
                                }
                            ],
                        },
                        {
                            "mode": "SUBWAY",
                            "route": {"shortName": "F"},
                            "from": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06N"}, "lat": 40.7597, "lon": -73.9532},
                            "to": {"name": "Lexington Av/63 St", "stop": {"gtfsId": "MTA_NYCT_Subway:B08S"}, "lat": 40.7644, "lon": -73.9656},
                            "startTime": 1755100800000,
                            "endTime": 1755101400000,
                            "realTime": True,
                            "arrivalDelay": 90,
                            "headsign": "96 St",
                            "steps": [],
                        },
                    ],
                }
            ]
        }
    }
}


@pytest.mark.asyncio
async def test_plan_route_parses_walk_steps_headsign_and_latlon():
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_OTP_RESPONSE_WITH_WALK_DATA
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        itineraries = await client.plan_route(40.7566, -73.9557, 40.7644, -73.9656)

    walk_leg = itineraries[0].legs[0]
    assert walk_leg.from_lat == 40.7566
    assert walk_leg.from_lon == -73.9557
    assert walk_leg.to_lat == 40.7597
    assert walk_leg.to_lon == -73.9532
    assert walk_leg.headsign is None
    assert len(walk_leg.steps) == 1
    assert walk_leg.steps[0].street_name == "Main St"
    assert walk_leg.steps[0].distance_meters == 120.5
    assert walk_leg.steps[0].relative_direction == "LEFT"
    assert walk_leg.steps[0].absolute_direction == "NORTH"
    assert walk_leg.steps[0].stay_on is False

    subway_leg = itineraries[0].legs[1]
    assert subway_leg.headsign == "96 St"
    assert subway_leg.steps == []


@pytest.mark.asyncio
async def test_plan_route_defaults_new_fields_when_absent():
    """Old-shaped responses (no headsign/steps/lat-lon keys) must not break parsing."""
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_OTP_RESPONSE  # the existing fixture, unchanged
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        itineraries = await client.plan_route(40.7597, -73.9532, 40.7644, -73.9656)

    leg = itineraries[0].legs[0]
    assert leg.headsign is None
    assert leg.steps == []
    assert leg.from_lat is None
    assert leg.to_lon is None


def test_old_shaped_stored_itinerary_snapshot_still_round_trips():
    """A monitored_trips.itinerary_snapshot row written before this task
    (no steps/headsign/lat-lon keys in its stored JSON at all) must still
    deserialize correctly -- this is the actual regression the design doc
    calls for: an old *stored* snapshot round-tripping through
    model_validate_json, not a fresh OTP response being parsed."""
    old_shaped_json = (
        '{"duration_seconds": 1800, "legs": [{"mode": "SUBWAY", "agency": "MTA", '
        '"route_short_name": "F", "from_stop_id": "mtasbwy:B06N", "from_stop_name": "Roosevelt Island", '
        '"to_stop_id": "mtasbwy:B08S", "to_stop_name": "Lex/63 St", "start_time_ms": 1755100800000, '
        '"end_time_ms": 1755101400000, "real_time": true, "arrival_delay_seconds": 90}]}'
    )
    itinerary = Itinerary.model_validate_json(old_shaped_json)
    assert itinerary.legs[0].route_short_name == "F"
    assert itinerary.legs[0].headsign is None
    assert itinerary.legs[0].steps == []
    assert itinerary.legs[0].from_lat is None
