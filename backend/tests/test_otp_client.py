import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from app.routing.otp_client import OTPClient

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
