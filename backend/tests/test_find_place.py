import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.find_place import find_place

SINGLE_MATCH_RESPONSE = {
    "features": [
        {
            "properties": {
                "lat": 40.753983,
                "lon": -74.0006028,
                "formatted": "30 Hudson Yards, 30 Hudson Boulevard East, New York, NY 10001, United States of America",
            }
        }
    ]
}

MULTI_MATCH_RESPONSE = {
    "features": [
        {"properties": {"lat": 40.7110715, "lon": -74.0086261, "formatted": "Zara, 222 Broadway, Manhattan"}},
        {"properties": {"lat": 40.7600172, "lon": -73.9764128, "formatted": "Zara, 666 West 52nd Street, Manhattan"}},
        {"properties": {"lat": 40.7743876, "lon": -73.9825127, "formatted": "Zara, 1963 Broadway, Manhattan"}},
    ]
}

NO_MATCH_RESPONSE = {"features": []}


@pytest.mark.asyncio
async def test_find_place_parses_a_single_real_match():
    mock_response = AsyncMock()
    mock_response.json = lambda: SINGLE_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        result = await find_place("hudson yards")

    assert result == [
        {
            "lat": 40.753983,
            "lon": -74.0006028,
            "formatted": "30 Hudson Yards, 30 Hudson Boulevard East, New York, NY 10001, United States of America",
        }
    ]
    call_kwargs = mock_get.await_args.kwargs
    assert call_kwargs["params"]["text"] == "hudson yards"
    assert "autocomplete" in str(mock_get.await_args)


@pytest.mark.asyncio
async def test_find_place_returns_all_real_candidates_when_ambiguous():
    mock_response = AsyncMock()
    mock_response.json = lambda: MULTI_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await find_place("zara manhattan")

    assert len(result) == 3
    assert result[0]["formatted"] == "Zara, 222 Broadway, Manhattan"
    assert result[2]["formatted"] == "Zara, 1963 Broadway, Manhattan"


@pytest.mark.asyncio
async def test_find_place_returns_empty_list_on_no_match():
    mock_response = AsyncMock()
    mock_response.json = lambda: NO_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await find_place("somewhere that does not exist")

    assert result == []


@pytest.mark.asyncio
async def test_find_place_returns_empty_list_on_http_error():
    with patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")):
        result = await find_place("anything")

    assert result == []
