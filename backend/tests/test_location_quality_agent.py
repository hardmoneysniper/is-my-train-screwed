import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic import APIConnectionError

from app.agents.location_quality_agent import check_location_matches

CANDIDATES = [
    {"lat": 40.71, "lon": -74.00, "formatted": "Shoe Repair, 21st St, Manhattan"},
    {"lat": 40.75, "lon": -73.99, "formatted": "Zara, 34th St, Manhattan"},
]


def _fake_usage():
    return MagicMock(model_dump=lambda: {"input_tokens": 50, "output_tokens": 10})


def _text_response(text: str):
    return MagicMock(content=[MagicMock(type="text", text=text)], usage=_fake_usage())


@pytest.mark.asyncio
async def test_drops_a_candidate_the_model_judges_a_clear_mismatch():
    response = _text_response(json.dumps({"keep_indices": [1]}))
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("the zara on 21st st manhattan", CANDIDATES)

    assert result == [CANDIDATES[1]]


@pytest.mark.asyncio
async def test_returns_empty_list_when_model_judges_nothing_plausible():
    response = _text_response(json.dumps({"keep_indices": []}))
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("a place that doesn't exist", CANDIDATES)

    assert result == []  # an intentional "none are good" result, not a fallback


@pytest.mark.asyncio
async def test_keeps_all_candidates_when_model_judges_all_plausible():
    response = _text_response(json.dumps({"keep_indices": [0, 1]}))
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("zara manhattan", CANDIDATES)

    assert result == CANDIDATES


@pytest.mark.asyncio
async def test_fails_open_to_unfiltered_candidates_on_api_error():
    with patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls:
        mock_cls.return_value.messages.create = AsyncMock(
            side_effect=APIConnectionError(request=MagicMock())
        )
        result = await check_location_matches("zara manhattan", CANDIDATES)

    assert result == CANDIDATES


@pytest.mark.asyncio
async def test_fails_open_to_unfiltered_candidates_on_malformed_model_response():
    response = _text_response("not json at all")
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("zara manhattan", CANDIDATES)

    assert result == CANDIDATES


@pytest.mark.asyncio
async def test_fails_open_when_keep_indices_is_not_a_list():
    response = _text_response(json.dumps({"keep_indices": "oops"}))
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("zara manhattan", CANDIDATES)

    assert result == CANDIDATES


@pytest.mark.asyncio
async def test_ignores_out_of_range_or_non_integer_indices():
    response = _text_response(json.dumps({"keep_indices": [1, 99, "x"]}))
    with (
        patch("app.agents.location_quality_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.location_quality_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await check_location_matches("zara manhattan", CANDIDATES)

    assert result == [CANDIDATES[1]]
