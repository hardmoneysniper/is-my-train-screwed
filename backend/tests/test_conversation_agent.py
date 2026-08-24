# backend/tests/test_conversation_agent.py
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
from app.agents.conversation_agent import ConversationAgent

@pytest.mark.asyncio
async def test_respond_calls_plan_route_tool_and_narrates_result():
    fake_tool_use_response = MagicMock(
        stop_reason="tool_use",
        content=[MagicMock(type="tool_use", name="plan_route", id="tool_1",
                            input={"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
    )
    fake_final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Take the F train — about 30 minutes.")],
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
