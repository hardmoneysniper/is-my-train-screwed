# backend/app/agents/conversation_agent.py
import json
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.routing.nearest_stop import get_stop_index
from app.agents.tools import FIND_STOP_TOOL, GET_RISK_TOOL, PLAN_ROUTE_TOOL
from app.models.transit import Itinerary
from app import risk_engine
import cost_guard

SYSTEM_PROMPT = (
    "You are a NYC transit trip advisor. You never invent routes, durations, "
    "or probabilities — always call plan_route and narrate its exact result. "
    "After planning a route, call get_risk to check its transfers. If it "
    "returns an empty list, there's no transfer to discuss. If it returns "
    "entries, narrate each using its exact p_miss/n/window_days; if an "
    "entry's quality is 'insufficient', say reliability data isn't "
    "available yet for that transfer rather than stating a number. "
    "If the user names a place instead of giving coordinates, call "
    "find_stop first to resolve it, then use the result's lat/lon with "
    "plan_route — never invent coordinates. "
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
            tools=[PLAN_ROUTE_TOOL, GET_RISK_TOOL, FIND_STOP_TOOL],
            messages=messages,
        )
        # Raises CostCapExceeded if this call pushed month-to-date spend at
        # or past the local cap -- deliberately left to propagate rather
        # than caught here, since a caught-and-swallowed cap breach would
        # silently let the agent keep responding past its budget.
        cost_guard.log_call(response.usage.model_dump(), agent="conversation_agent")
        return response

    def _handle_get_risk(self, tool_input: dict, last_itineraries: list[Itinerary]) -> str:
        # last_itineraries is scoped to this respond() call only -- it is a
        # local variable in respond(), never instance/module state, so it
        # never leaks an itinerary across separate requests (see
        # task-7-brief.md's explicit scope boundary).
        itinerary_index = tool_input.get("itinerary_index", 0)
        if not (0 <= itinerary_index < len(last_itineraries)):
            return json.dumps({"error": "no itinerary available yet — call plan_route first"})
        itinerary = last_itineraries[itinerary_index]
        risks = risk_engine.get_risk(itinerary)
        return json.dumps([r.model_dump() for r in risks])

    async def respond(self, user_message: str, conversation_history: list[dict]) -> str:
        messages = conversation_history + [{"role": "user", "content": user_message}]

        response = await self._create(messages)
        last_itineraries: list[Itinerary] = []

        while response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            for tool_use in tool_uses:
                if tool_use.name == "plan_route":
                    itineraries = await self._otp.plan_route(**tool_use.input)
                    last_itineraries = itineraries
                    content = json.dumps([it.model_dump() for it in itineraries])
                elif tool_use.name == "get_risk":
                    content = self._handle_get_risk(tool_use.input, last_itineraries)
                elif tool_use.name == "find_stop":
                    matches = get_stop_index().find_by_name(tool_use.input["query"])
                    content = json.dumps(matches)
                else:
                    content = json.dumps({"error": f"unknown tool: {tool_use.name}"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": content,
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
