# backend/app/agents/conversation_agent.py
import json
from datetime import datetime, timezone
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.routing.nearest_stop import get_stop_index
from app.agents.tools import (
    CANCEL_MONITORED_TRIP_TOOL,
    CREATE_MONITORED_TRIP_TOOL,
    FIND_STOP_TOOL,
    GET_RISK_TOOL,
    PLAN_ROUTE_TOOL,
)
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary
from app import deadline, monitoring, risk_engine
from db import get_connection
import cost_guard

# Shared with app/agents/replan_agent.py, which formats this same footer in
# a plain-Python (no-LLM) notification template -- see task-6-brief.md.
# Extracted here rather than duplicated so the literal only exists once.
CITATION_FOOTER_TEMPLATE = "*Based on {n} observed patterns in the last {window_days} days.*"

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
    "Keep answers to 1-3 sentences.\n\n"
    "Citation format: every percentage you state gets a trailing '*' "
    "directly on the number, e.g. '30%*' — not '30%', not '*30%'. If (and "
    "only if) your answer cites at least one get_risk entry with "
    "quality 'ok', end your response with exactly one footer line, on its "
    "own line, wrapped in single asterisks: "
    f"'{CITATION_FOOTER_TEMPLATE}' "
    "— substitute that entry's real n and window_days, never invented "
    "numbers. If you're citing more than one 'ok' entry, use only ONE "
    "footer, for whichever probability is your primary answer — never "
    "combine n/window_days pairs and never emit more than one footer line. "
    "If every relevant entry has quality 'insufficient', state no "
    "percentage and add no footer.\n"
    "Example — user asks \"What's the risk transferring from the F to the "
    "Q at Roosevelt Island?\" and get_risk returns "
    "{p_miss: 0.12, n: 500, window_days: 14, quality: 'ok'}: you respond "
    "exactly in this shape: \"There's about a 12%* chance of missing that "
    "transfer.\\n*Based on 500 observed patterns in the last 14 days.*\"\n\n"
    "Monitoring a trip: after checking get_risk, if any entry has quality \"ok\"\n"
    "(any real percentage, however small), or the trip sounds deadline-sensitive\n"
    "(the user mentions a flight, interview, appointment, or says they're short\n"
    "on time), ask the user if they'd like you to monitor the trip for\n"
    "disruptions. Never offer if get_risk returned an empty list AND there's no\n"
    "deadline signal. Only call create_monitored_trip after the user agrees.\n\n"
    "If the user gives a specific deadline (e.g. \"I need to be there by 6pm\",\n"
    "\"my flight boards at 9:15\"), convert it to that day's date (using the\n"
    "[Current time: ...] marker on the message) and pass it as create_monitored_trip's\n"
    "deadline_ts, in epoch milliseconds. If the deadline is unclear or you're not\n"
    "sure what date/time they mean, ask rather than guessing — never invent a\n"
    "deadline_ts from an ambiguous statement.\n\n"
    "After create_monitored_trip succeeds, tell the user you're monitoring the\n"
    "trip. If its result includes a non-null depart_by_ts, also tell them the\n"
    "latest safe departure time (format the epoch-millisecond timestamp as a\n"
    "local clock time, the same way you already format itinerary leg times) —\n"
    "this is real data from the trip's historical delay patterns, not a guess.\n"
    "If depart_by_ts is null, don't mention a departure time at all — don't\n"
    "explain why, just omit it.\n\n"
    "Recognize when the user is done with a monitored trip — phrases like \"I'm\n"
    "here,\" \"I made it,\" \"cancel this trip,\" \"stop monitoring\" — and call\n"
    "cancel_monitored_trip. If its result has \"error\": \"ambiguous\", it means the\n"
    "user has more than one active monitored trip; list the real trips from its\n"
    "active_trips field and ask which one they mean, then call\n"
    "cancel_monitored_trip again with the trip_id they pick. Never guess which\n"
    "trip they mean and never cancel more than one trip for one request."
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


def _trip_summary(trip: MonitoredTrip) -> dict:
    """A small, server-built, real-fields-only summary for the LLM to relay
    verbatim when asking the user to disambiguate between multiple active
    monitored trips -- the LLM only quotes this back, it never invents the
    text itself."""
    legs = trip.itinerary_snapshot.legs
    return {
        "trip_id": trip.id,
        "summary": f"{legs[0].route_short_name} to {legs[-1].to_stop_name}, started {trip.created_at.isoformat()}",
    }


class ConversationAgent:
    def __init__(self):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._otp = OTPClient(base_url=settings.otp_base_url)

    async def _create(self, messages: list[dict]):
        response = await self._client.messages.create(
            model=settings.conversation_agent_model,
            max_tokens=512,
            system=_SYSTEM_BLOCKS,
            tools=[
                PLAN_ROUTE_TOOL,
                GET_RISK_TOOL,
                FIND_STOP_TOOL,
                CREATE_MONITORED_TRIP_TOOL,
                CANCEL_MONITORED_TRIP_TOOL,
            ],
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

    def _handle_create_monitored_trip(
        self, tool_input: dict, last_itineraries: list[Itinerary], anonymous_id: str
    ) -> str:
        itinerary_index = tool_input.get("itinerary_index", 0)
        if not (0 <= itinerary_index < len(last_itineraries)):
            return json.dumps({"error": "no itinerary available yet — call plan_route first"})
        itinerary = last_itineraries[itinerary_index]
        deadline_ts = tool_input.get("deadline_ts")
        trip_id = monitoring.create_monitored_trip(itinerary, anonymous_id, deadline_ts)
        # depart_by_ts is computed separately from create_monitored_trip, here
        # in dispatch code, rather than inside that function -- this keeps
        # create_monitored_trip's own signature exactly `-> int` (per the
        # plan) while still giving the agent a real, tool-computed number to
        # narrate (never LLM-invented) if the user wants to know when to
        # leave. It's null both when there's no deadline and when
        # compute_deadline_threshold itself returns None for insufficient
        # data -- SYSTEM_PROMPT tells the agent to treat both cases
        # identically (omit the departure time, no explanation).
        depart_by_ts = None
        if deadline_ts is not None:
            depart_by_ts = deadline.compute_deadline_threshold(itinerary, deadline_ts)
        return json.dumps({"trip_id": trip_id, "depart_by_ts": depart_by_ts})

    def _handle_cancel_monitored_trip(self, tool_input: dict, anonymous_id: str) -> str:
        trip_id = tool_input.get("trip_id")
        if trip_id is None:
            # Short-lived, independent connection -- not threaded through
            # respond()'s signature, matching the design doc's "one short
            # transaction, not one spanning" principle.
            conn = get_connection()
            try:
                active = monitoring.list_active_trips(conn, anonymous_id)
            finally:
                conn.close()
            if len(active) == 0:
                return json.dumps({"error": "no active trip to cancel"})
            if len(active) > 1:
                return json.dumps({
                    "error": "ambiguous",
                    "active_trips": [_trip_summary(t) for t in active],
                })
            trip_id = active[0].id
        cancelled = monitoring.cancel_monitored_trip(trip_id, anonymous_id)
        return json.dumps({"cancelled": cancelled, "trip_id": trip_id})

    async def respond(self, user_message: str, conversation_history: list[dict], anonymous_id: str) -> str:
        # Current time is injected into this per-call user message only --
        # never into the cached SYSTEM_PROMPT/_SYSTEM_BLOCKS, which must stay
        # byte-identical across calls for Anthropic's prompt caching to work
        # (see the comment above _SYSTEM_BLOCKS). Interpolating datetime.now()
        # into the system block would silently break that caching on every
        # call. The LLM needs today's date/time to parse a stated deadline
        # ("by 6pm") into a real epoch-ms timestamp.
        now_iso = datetime.now(timezone.utc).isoformat()
        messages = conversation_history + [
            {"role": "user", "content": f"[Current time: {now_iso}]\n{user_message}"}
        ]

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
                elif tool_use.name == "create_monitored_trip":
                    content = self._handle_create_monitored_trip(tool_use.input, last_itineraries, anonymous_id)
                elif tool_use.name == "cancel_monitored_trip":
                    content = self._handle_cancel_monitored_trip(tool_use.input, anonymous_id)
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
