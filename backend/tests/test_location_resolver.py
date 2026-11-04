from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic import APIConnectionError

from app.location_resolver import resolve_location_description
from cost_guard import CostCapExceeded


def _fake_usage():
    return MagicMock(model_dump=lambda: {"input_tokens": 50, "output_tokens": 10})


def _text_response(text: str):
    return MagicMock(content=[MagicMock(type="text", text=text)], usage=_fake_usage())


@pytest.mark.asyncio
async def test_returns_the_rewritten_string_on_success():
    response = _text_response("Hudson Yards, Manhattan, New York, NY")
    with patch("app.location_resolver.AsyncAnthropic") as mock_cls, \
         patch("app.location_resolver.cost_guard.log_call") as mock_log:
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await resolve_location_description("hudson yards")

    assert result == "Hudson Yards, Manhattan, New York, NY"
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["agent"] == "location_resolver"


@pytest.mark.asyncio
async def test_falls_back_to_original_query_on_api_error():
    with patch("app.location_resolver.AsyncAnthropic") as mock_cls:
        mock_cls.return_value.messages.create = AsyncMock(
            side_effect=APIConnectionError(request=MagicMock())
        )
        result = await resolve_location_description("the zara on 21st st")

    assert result == "the zara on 21st st"


@pytest.mark.asyncio
async def test_falls_back_to_original_query_on_empty_response_text():
    response = _text_response("   ")
    with patch("app.location_resolver.AsyncAnthropic") as mock_cls, \
         patch("app.location_resolver.cost_guard.log_call"):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await resolve_location_description("some place")

    assert result == "some place"


@pytest.mark.asyncio
async def test_cost_cap_exceeded_propagates_instead_of_falling_back():
    response = _text_response("Hudson Yards, Manhattan, New York, NY")
    with patch("app.location_resolver.AsyncAnthropic") as mock_cls, \
         patch("app.location_resolver.cost_guard.log_call", side_effect=CostCapExceeded("over cap")):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        with pytest.raises(CostCapExceeded):
            await resolve_location_description("hudson yards")
