from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.anthropic_text import call_and_log, extract_json_object, extract_text
from cost_guard import CostCapExceeded


def _response(blocks):
    return MagicMock(content=blocks)


def test_extract_text_returns_the_first_text_blocks_text():
    response = _response([MagicMock(type="text", text="hello")])
    assert extract_text(response) == "hello"


def test_extract_text_returns_empty_string_when_no_text_block_exists():
    response = _response([MagicMock(type="tool_use")])
    assert extract_text(response) == ""


def test_extract_text_skips_a_leading_non_text_block():
    response = _response([MagicMock(type="tool_use"), MagicMock(type="text", text="the real text")])
    assert extract_text(response) == "the real text"


def test_extract_json_object_parses_bare_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_strips_surrounding_prose():
    assert extract_json_object('Sure, here it is: {"a": 1} -- hope that helps!') == {"a": 1}


def test_extract_json_object_strips_a_markdown_fence():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_object_raises_value_error_when_no_object_found():
    with pytest.raises(ValueError):
        extract_json_object("not json at all")


def _fake_usage():
    return MagicMock(model_dump=lambda: {"input_tokens": 10, "output_tokens": 5})


@pytest.mark.asyncio
async def test_call_and_log_passes_through_model_and_messages():
    response = MagicMock(usage=_fake_usage())
    with patch("app.anthropic_text.AsyncAnthropic") as mock_cls, \
         patch("app.anthropic_text.cost_guard.log_call") as mock_log:
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        result = await call_and_log("claude-haiku-4-5-20251001", 100, [{"role": "user", "content": "hi"}], agent="test_agent")

    assert result is response
    mock_cls.return_value.messages.create.assert_awaited_once_with(
        model="claude-haiku-4-5-20251001", max_tokens=100, messages=[{"role": "user", "content": "hi"}],
    )
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["agent"] == "test_agent"
    assert mock_log.call_args.kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_call_and_log_includes_system_only_when_given():
    response = MagicMock(usage=_fake_usage())
    with patch("app.anthropic_text.AsyncAnthropic") as mock_cls, \
         patch("app.anthropic_text.cost_guard.log_call"):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        await call_and_log("m", 50, [{"role": "user", "content": "hi"}], agent="a", system="be concise")

    call_kwargs = mock_cls.return_value.messages.create.call_args.kwargs
    assert call_kwargs["system"] == "be concise"


@pytest.mark.asyncio
async def test_call_and_log_propagates_cost_cap_exceeded():
    response = MagicMock(usage=_fake_usage())
    with patch("app.anthropic_text.AsyncAnthropic") as mock_cls, \
         patch("app.anthropic_text.cost_guard.log_call", side_effect=CostCapExceeded("over cap")):
        mock_cls.return_value.messages.create = AsyncMock(return_value=response)
        with pytest.raises(CostCapExceeded):
            await call_and_log("m", 50, [{"role": "user", "content": "hi"}], agent="a")
