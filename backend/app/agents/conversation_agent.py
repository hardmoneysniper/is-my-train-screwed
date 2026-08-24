# backend/app/agents/conversation_agent.py
import json
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.agents.tools import PLAN_ROUTE_TOOL

SYSTEM_PROMPT = (
    "You are a NYC transit trip advisor. You never invent routes, durations, "
    "or probabilities — always call plan_route and narrate its exact result. "
    "Keep answers to 1-3 sentences."
)


class ConversationAgent:
    def __init__(self):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._otp = OTPClient(base_url=settings.otp_base_url)

    async def respond(self, user_message: str, conversation_history: list[dict]) -> str:
        messages = conversation_history + [{"role": "user", "content": user_message}]

        response = await self._client.messages.create(
            model=settings.conversation_agent_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[PLAN_ROUTE_TOOL],
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            tool_use = next(b for b in response.content if b.type == "tool_use")
            itineraries = await self._otp.plan_route(**tool_use.input)
            tool_result = {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps([it.model_dump() for it in itineraries]),
                }],
            }
            messages = messages + [
                {"role": "assistant", "content": response.content},
                tool_result,
            ]
            response = await self._client.messages.create(
                model=settings.conversation_agent_model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=[PLAN_ROUTE_TOOL],
                messages=messages,
            )

        text_block = next(b for b in response.content if b.type == "text")
        return text_block.text
