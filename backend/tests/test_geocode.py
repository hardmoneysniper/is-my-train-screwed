import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.geocode import geocode_address

EXACT_MATCH_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "status": "EXACT_MATCH",
            "response": {
                "latitude": 40.756552,
                "longitude": -73.955881,
                "firstBoroughName": "MANHATTAN",
                "xCoordinate": "999999",  # real field present in the live response, must NOT be used
                "yCoordinate": "888888",
            },
        }
    ],
}

REJECTED_RESPONSE = {"status": "REJECTED", "results": []}


@pytest.mark.asyncio
async def test_geocode_address_parses_exact_match():
    mock_response = AsyncMock()
    mock_response.json = lambda: EXACT_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        result = await geocode_address("2 west loop road manhattan")

    assert result == {"lat": 40.756552, "lon": -73.955881}
    call_kwargs = mock_get.await_args.kwargs
    assert call_kwargs["params"]["input"] == "2 west loop road manhattan"


@pytest.mark.asyncio
async def test_geocode_address_returns_none_on_rejected():
    mock_response = AsyncMock()
    mock_response.json = lambda: REJECTED_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await geocode_address("roosevelt island")  # colloquial, real no-match per design doc

    assert result is None


@pytest.mark.asyncio
async def test_geocode_address_returns_none_on_http_error():
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")):
        result = await geocode_address("anything")

    assert result is None
