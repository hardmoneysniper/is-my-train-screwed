# backend/app/agents/conversation_agent.py
import json
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.agents.tools import PLAN_ROUTE_TOOL
import cost_guard

SYSTEM_PROMPT = (
    "You are a NYC transit trip advisor. You never invent routes, durations, "
    "or probabilities — always call plan_route and narrate its exact result. "
    "Keep answers to 1-3 sentences."
)

# system + tools are identical on every turn, so they're cached as one unit.
# Anthropic builds cache prefixes in the order tools -> system -> messages,
# so a single breakpoint on the LAST system block covers both tools and
# system (a breakpoint placed on the tool instead would cache only the
# tools, leaving the system prompt uncached on every call).
_SYSTEM_BLOCKS = [{
    "type": "text",
    "text": SYSTEM_PROMPT,
    "cache_control": {"type": "ephemeral"},
}]


class ConversationAgent:
    def __init__(self):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._otp = OTPClient(base_url=settings.otp_base_url)

    async def _create(self, messages: list[dict]):
        response = await self._client.messages.create(
            model=settings.conversation_agent_model,
            max_tokens=512,
            system=_SYSTEM_BLOCKS,
            tools=[PLAN_ROUTE_TOOL],
            messages=messages,
        )
        # Raises CostCapExceeded if this call pushed month-to-date spend at
        # or past the local cap -- deliberately left to propagate rather
        # than caught here, since a caught-and-swallowed cap breach would
        # silently let the agent keep responding past its budget.
        cost_guard.log_call(response.usage.model_dump(), agent="conversation_agent")
        return response

    async def respond(self, user_message: str, conversation_history: list[dict]) -> str:
        messages = conversation_history + [{"role": "user", "content": user_message}]

        response = await self._create(messages)

        while response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            for tool_use in tool_uses:
                itineraries = await self._otp.plan_route(**tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps([it.model_dump() for it in itineraries]),
                })
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            response = await self._create(messages)

        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            return ""
        return text_block.text
