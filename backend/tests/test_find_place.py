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
    expected = [
        {
            "lat": 40.753983,
            "lon": -74.0006028,
            "formatted": "30 Hudson Yards, 30 Hudson Boulevard East, New York, NY 10001, United States of America",
        }
    ]
    with (
        patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get,
        patch("app.find_place.check_location_matches", new_callable=AsyncMock, return_value=expected),
    ):
        result = await find_place("hudson yards")

    assert result == expected
    call_kwargs = mock_get.await_args.kwargs
    assert call_kwargs["params"]["text"] == "hudson yards"
    assert "autocomplete" in str(mock_get.await_args)


@pytest.mark.asyncio
async def test_find_place_returns_all_real_candidates_when_ambiguous():
    mock_response = AsyncMock()
    mock_response.json = lambda: MULTI_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    expected = [
        {"lat": 40.7110715, "lon": -74.0086261, "formatted": "Zara, 222 Broadway, Manhattan"},
        {"lat": 40.7600172, "lon": -73.9764128, "formatted": "Zara, 666 West 52nd Street, Manhattan"},
        {"lat": 40.7743876, "lon": -73.9825127, "formatted": "Zara, 1963 Broadway, Manhattan"},
    ]
    with (
        patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response),
        patch("app.find_place.check_location_matches", new_callable=AsyncMock, return_value=expected),
    ):
        result = await find_place("zara manhattan")

    assert len(result) == 3
    assert result[0]["formatted"] == "Zara, 222 Broadway, Manhattan"
    assert result[2]["formatted"] == "Zara, 1963 Broadway, Manhattan"


@pytest.mark.asyncio
async def test_find_place_runs_the_quality_check_against_the_original_query_not_the_geocoded_one():
    # The quality check must compare candidates against the RIDER's own
    # wording (original_query), not whatever location_resolver.py may have
    # rewritten it to -- see find_place's docstring for why.
    mock_response = AsyncMock()
    mock_response.json = lambda: SINGLE_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with (
        patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response),
        patch(
            "app.find_place.check_location_matches", new_callable=AsyncMock, return_value=[]
        ) as mock_check,
    ):
        result = await find_place("30 Hudson Yards", original_query="that mall thing by the rail yards")

    assert mock_check.await_args.args[0] == "that mall thing by the rail yards"
    assert result == []  # the quality check judged nothing plausible -- honest empty result, not a fallback


@pytest.mark.asyncio
async def test_find_place_defaults_original_query_to_query_when_not_given():
    mock_response = AsyncMock()
    mock_response.json = lambda: SINGLE_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with (
        patch("app.find_place.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response),
        patch(
            "app.find_place.check_location_matches", new_callable=AsyncMock, return_value=[]
        ) as mock_check,
    ):
        await find_place("hudson yards")

    assert mock_check.await_args.args[0] == "hudson yards"


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
