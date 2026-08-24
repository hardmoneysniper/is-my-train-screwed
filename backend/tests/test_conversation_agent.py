# backend/tests/test_conversation_agent.py
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import cost_guard
from app.agents.conversation_agent import ConversationAgent


def _fake_usage(input_tokens=100, output_tokens=20, cache_creation=0, cache_read=0):
    return MagicMock(model_dump=MagicMock(return_value={
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }))


@pytest.fixture(autouse=True)
def isolated_cost_log(tmp_path, monkeypatch):
    # Every ConversationAgent call now logs to cost_guard -- redirect its
    # log file so these tests never touch the real local spend log, and
    # reset the spend cap to a value nothing in this file's small fixture
    # usage would ever hit.
    monkeypatch.setattr(cost_guard, "LOG_PATH", tmp_path / "cost_log.ndjson")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5")


@pytest.mark.asyncio
async def test_respond_calls_plan_route_tool_and_narrates_result():
    fake_tool_use_response = MagicMock(
        stop_reason="tool_use",
        content=[MagicMock(type="tool_use", name="plan_route", id="tool_1",
                            input={"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    fake_final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Take the F train — about 30 minutes.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = []
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[fake_tool_use_response, fake_final_response])

        agent = ConversationAgent()
        reply = await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[])

    assert reply == "Take the F train — about 30 minutes."
    mock_plan.assert_called_once()

    # system + tools are cached as one prefix: the breakpoint must sit on
    # the last (only) system block, not on the tool -- see the comment in
    # conversation_agent.py for why the ordering matters.
    call_kwargs = mock_client.messages.create.call_args_list[0].kwargs
    assert call_kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in call_kwargs["tools"][0]


@pytest.mark.asyncio
async def test_respond_propagates_cost_cap_exceeded():
    # If the local spend cap is already exhausted, the very next call's
    # cost_guard.log_call() must raise -- and that must propagate out of
    # respond() rather than being swallowed into a normal reply, since a
    # silently-caught cap breach would let the agent keep spending past
    # its budget.
    fake_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="irrelevant")],
        usage=_fake_usage(input_tokens=10_000_000),  # $10 of input alone, over the $5 cap
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(return_value=fake_response)

        agent = ConversationAgent()
        with pytest.raises(cost_guard.CostCapExceeded):
            await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[])
