# backend/app/agents/conversation_agent.py
import asyncio
import json
import logging
from datetime import datetime, timezone
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.routing.nearest_stop import get_stop_index
from app.agents.tools import (
    CANCEL_MONITORED_TRIP_TOOL,
    CREATE_MONITORED_TRIP_TOOL,
    FIND_ADDRESS_TOOL,
    FIND_PLACE_TOOL,
    FIND_STOP_TOOL,
    GET_RISK_TOOL,
    PLAN_ROUTE_TOOL,
)
from app.agents.feedback_triage_agent import triage_feedback
from app.find_place import find_place
from app.location_resolver import resolve_location_description
from app.geocode import geocode_address
from app.otp_lifecycle import ensure_otp_awake
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary
from app import deadline, feedback, monitoring, risk_engine
from db import claim_pending_notifications, get_connection
import cost_guard

# Feedback Triage Agent (2026-11-06): both deterministic, plain-Python
# strings, never LLM-authored -- same discipline as every other
# system-composed (non-narrated) text in this product.
FEEDBACK_REQUEST_TEXT = (
    "How did that trip go? Reply with any quick feedback -- what worked, "
    "what didn't -- and we'll use it to make this better."
)
FEEDBACK_THANK_YOU_REPLY = "Thanks for the feedback -- it really helps. Have a great day!"


async def _run_feedback_triage(feedback_id: int) -> None:
    """Fire-and-forget wrapper (see respond()'s capture branch below) --
    logs a failure instead of letting it vanish as an unretrieved
    exception on the background task, the same visibility concern
    main.py's own basicConfig comment already raised for this service."""
    try:
        await triage_feedback(feedback_id)
    except Exception:
        logging.exception(f"feedback triage failed for feedback_id={feedback_id}")

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
    "plan_route — never invent coordinates. If the user refers to their "
    "current location (e.g. 'from here', 'near me') and a "
    "[User's current location: ...] marker is present on the message, use "
    "those coordinates directly with plan_route — never call find_stop for "
    "this case. If they reference their current location but no marker is "
    "present, ask them to share their location or name a place instead. "
    "If the user types a street address, call find_address to resolve it, "
    "then use the result's lat/lon with plan_route — if it returns null, "
    "try find_place next (see below) rather than giving up immediately. "
    "Keep answers to 1-3 sentences.\n\n"
    "find_place fallback: if BOTH find_stop and find_address return no "
    "match for a free-text origin or destination, call find_place — a "
    "broader business/landmark-name search that already normalizes vague "
    "or colloquial wording internally before geocoding it, so pass the "
    "user's description through as-is. It returns a LIST of candidates, "
    "not one answer — if it returns more than one, list the distinct "
    "matches (using each one's real formatted address) and ask the user "
    "which they meant before proceeding, e.g. \"I found a few matches — "
    "did you mean the Zara at 222 Broadway, or at 1963 Broadway?\" If it "
    "returns exactly one candidate, treat it the same as a find_stop/"
    "find_address match and confirm it the usual way below. If it "
    "returns an empty list, tell them you couldn't find that place and "
    "ask them to clarify or try a stop name or exact address instead — "
    "never invent coordinates.\n\n"
    "Confirming a resolved location: whenever you resolve an origin or "
    "destination via find_stop, find_address, or find_place (never for "
    "the user's exact device location marker — that's already exact, no "
    "confirmation needed), show them exactly what you resolved it to "
    "(the real stop name, matched address, or matched place) and ask "
    "them to confirm before calling plan_route, e.g. \"Did you mean "
    "Lexington Av/63 St? Reply YES if this is correct.\" Only call "
    "plan_route once they confirm. If they say no or give a correction, "
    "resolve the new answer the same way and ask again — never guess "
    "which one they meant. This applies every time a free-text origin or "
    "destination gets resolved this way, not just on a trip's first "
    "request — including resolving a new destination for a reroute or "
    "mid-trip change below. If a trip already has both an origin and a "
    "destination confirmed and the user corrects only one of them, "
    "re-resolve only the one they corrected — leave the other exactly as "
    "already confirmed — then show BOTH together in the new confirmation "
    "message (e.g. \"Got it — from Roosevelt Island to 150 West 22nd "
    "Street. Reply YES if this is correct.\"), not just the changed one "
    "in isolation, so they can review the full trip before you call "
    "plan_route again.\n\n"
    "Walking directions: when an itinerary includes a WALK leg, condense its "
    "steps into 1-2 natural sentences using the real street names, distances, "
    "and turns it contains — don't mechanically recite every step, and don't "
    "invent a street name it didn't give you (a generic value like 'sidewalk' "
    "stays generic, e.g. 'continue along the sidewalk'). At every boarding and "
    "every transfer, state 'board the {route} toward **{headsign}**' using the "
    "real headsign text, with the headsign in bold markdown — this applies to "
    "every boarding, not only where get_risk has a citable number, since it's "
    "navigation guidance, not a risk statement.\n\n"
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
    "Wait time and rerouting: a get_risk entry's p_miss (the citable "
    "percentage above) and typical_wait_seconds are two SEPARATE, "
    "independently-available numbers — never treat one as implying the "
    "other is available. typical_wait_seconds is real data (your next "
    "option's typical wait) even on entries where quality is "
    "'insufficient' and no percentage can be cited. Whenever "
    "typical_wait_seconds is not null, mention it in the same breath as "
    "the percentage (never the percentage alone when a wait time exists — "
    "a bare miss percentage reads scarier than it should without knowing "
    "how long the next option actually takes), convert it from seconds to "
    "whole minutes for the user. If typical_wait_seconds is null, say "
    "nothing about wait time; don't guess it.\n"
    "Whenever an entry has p_miss and/or typical_wait_seconds citable "
    "(i.e. not both null), end your response by asking whether they'd "
    "like a faster route calculated instead — unconditionally, every "
    "time either one is citable, no matter how short or long the wait is "
    "or how low the miss percentage is. Never calculate or suggest a "
    "faster route unprompted — only ever in response to this ask being "
    "answered, or the user raising it themselves (see below).\n"
    "Example — get_risk returns {p_miss: 0.55, n: 400, window_days: 20, "
    "quality: 'ok', typical_wait_seconds: 480}: "
    "\"There's about a 55%* chance of missing that transfer, but your next "
    "option typically arrives in 8 minutes — want me to calculate a faster "
    "route instead?\\n*Based on 400 observed patterns in the last 20 "
    "days.*\" Example — {quality: 'insufficient', typical_wait_seconds: "
    "300}: \"I don't have enough reliability data for that transfer yet, "
    "but your next option typically arrives in 5 minutes — want me to "
    "calculate a faster route instead?\" (no footer, since quality isn't "
    "'ok').\n\n"
    "Calculating the faster route: only ever do this when the user agrees "
    "to the reroute offer, or independently tells you BOTH that they've "
    "already missed the transfer AND that they can't/won't wait for the "
    "next one — missing it alone, without that second signal, still gets "
    "the ask above instead of an automatic reroute. When it does trigger: "
    "if they have an active monitored trip, first call "
    "cancel_monitored_trip with no trip_id (same as the Mid-trip "
    "destination change flow below — it auto-resolves their active trip) "
    "so the Trip Monitor never keeps tracking the now-stale original "
    "itinerary. Then call plan_route fresh from their current location "
    "(resolve it the same way as any other request — use a [User's "
    "current location: ...] marker if present, otherwise ask) to their "
    "original destination, and narrate whatever OTP actually returns as "
    "the fastest option — never estimate, never assume it must differ "
    "from their original route (the fastest real option might "
    "legitimately be the same one), and never reference the historical "
    "get_risk data when deciding what to recommend here. Then fall "
    "through to this prompt's normal get_risk/monitoring-offer logic for "
    "the new itinerary, exactly as for any new trip.\n\n"
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
    "trip they mean and never cancel more than one trip for one request.\n\n"
    "Mid-trip destination change: if the user changes their destination while "
    "a trip is active (e.g. 'actually take me to X instead'), first call "
    "cancel_monitored_trip with no trip_id (it auto-resolves their active "
    "trip), then get their current location the same way as any other "
    "current-location reference (use it if already present on the message, "
    "otherwise ask for it), then call plan_route fresh from that location to "
    "the newly-named destination (resolving it via find_stop/find_address/"
    "find_place same as any new trip request), then fall through to this prompt's "
    "normal monitoring-offer logic for the new trip."
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
                FIND_ADDRESS_TOOL,
                FIND_PLACE_TOOL,
                CREATE_MONITORED_TRIP_TOOL,
                CANCEL_MONITORED_TRIP_TOOL,
            ],
            messages=messages,
        )
        # Raises CostCapExceeded if this call pushed month-to-date spend at
        # or past the local cap -- deliberately left to propagate rather
        # than caught here, since a caught-and-swallowed cap breach would
        # silently let the agent keep responding past its budget.
        cost_guard.log_call(response.usage.model_dump(), agent="conversation_agent", model=settings.conversation_agent_model)
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

    async def respond(
        self, user_message: str, conversation_history: list[dict], anonymous_id: str,
        user_location: dict | None = None,
    ) -> str:
        # Current time is injected into this per-call user message only --
        # never into the cached SYSTEM_PROMPT/_SYSTEM_BLOCKS, which must stay
        # byte-identical across calls for Anthropic's prompt caching to work
        # (see the comment above _SYSTEM_BLOCKS). Interpolating datetime.now()
        # into the system block would silently break that caching on every
        # call. The LLM needs today's date/time to parse a stated deadline
        # ("by 6pm") into a real epoch-ms timestamp. user_location follows
        # the identical pattern, for the identical reason.
        # Feedback capture (2026-11-06): if this rider is currently
        # awaiting a response to a just-delivered feedback request (see
        # the ask appended at the bottom of this method), their message
        # IS that feedback -- unconditionally, by the user's explicit
        # design (see app/feedback.py's module docstring for why no LLM
        # judgment call is needed here). Short-circuits before the OTP
        # wake-trigger and before any LLM call at all: capturing feedback
        # costs nothing beyond a DB write plus a background triage call.
        conn = get_connection()
        try:
            captured = feedback.claim_awaiting_feedback_response(conn, anonymous_id, user_message)
        finally:
            conn.close()
        if captured is not None:
            asyncio.create_task(_run_feedback_triage(captured["id"]))
            return FEEDBACK_THANK_YOU_REPLY

        # 2026-10-15 (user): wake OTP the moment a user starts a NEW
        # conversation (empty history, same signal the PWA already uses
        # for "is this a first message" -- no new tracking needed),
        # fire-and-forget, before the LLM call even starts. The time the
        # user spends reading the reply and typing their next message is
        # the real cold-start mask, same reasoning already proven out for
        # the (now-abandoned) SMS channel design. A returning user mid-
        # conversation has almost certainly already triggered this via
        # their first message, so there's no reason to repeat it every
        # turn.
        if not conversation_history:
            asyncio.create_task(ensure_otp_awake())

        now_iso = datetime.now(timezone.utc).isoformat()
        prefix = f"[Current time: {now_iso}]"
        if user_location is not None:
            prefix += f"\n[User's current location: {user_location['lat']}, {user_location['lon']}]"
        messages = conversation_history + [
            {"role": "user", "content": f"{prefix}\n{user_message}"}
        ]

        response = await self._create(messages)
        last_itineraries: list[Itinerary] = []

        while response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            for tool_use in tool_uses:
                if tool_use.name == "plan_route":
                    if await ensure_otp_awake():
                        itineraries = await self._otp.plan_route(**tool_use.input)
                        last_itineraries = itineraries
                        content = json.dumps([it.model_dump() for it in itineraries])
                    else:
                        content = json.dumps({
                            "error": "the routing engine is still starting up, ask the user to try again in a few seconds"
                        })
                elif tool_use.name == "get_risk":
                    content = self._handle_get_risk(tool_use.input, last_itineraries)
                elif tool_use.name == "find_stop":
                    matches = get_stop_index().find_by_name(tool_use.input["query"])
                    content = json.dumps(matches)
                elif tool_use.name == "find_address":
                    result = await geocode_address(tool_use.input["address"])
                    content = json.dumps(result)
                elif tool_use.name == "find_place":
                    original_query = tool_use.input["query"]
                    resolved_query = await resolve_location_description(original_query)
                    place_result = await find_place(resolved_query, original_query)
                    content = json.dumps(place_result)
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
        reply_text = text_block.text if text_block is not None else ""

        # Claim happens LAST, only once the reply is guaranteed to be
        # returned -- never at the top of respond(), before the LLM turn(s).
        # claim_pending_notifications is destructive (it atomically clears
        # pending_notification the moment it's called): if it ran earlier
        # and something later in this same call raised (a cost-cap breach,
        # a tool dispatch error), the notification would be gone from the
        # database forever, even though the request that was supposed to
        # show it never actually completed. Claiming here means a failed
        # request leaves the notification untouched for a later, successful
        # call to pick up honestly -- see task-8-brief.md.
        conn = get_connection()
        try:
            claimed = claim_pending_notifications(conn, anonymous_id)
            feedback_prompt = feedback.claim_unasked_feedback_prompt(conn, anonymous_id)
        finally:
            conn.close()

        # Claimed notification text was already fully composed as plain,
        # final, user-ready text by Task 6's replan_trip -- it is never fed
        # back through the LLM for narration. Re-narrating it risks the
        # model paraphrasing or reformatting a citation number, which this
        # product's hard rule forbids (see CLAUDE.md). Pure string
        # concatenation, zero new LLM calls. The feedback ask (if any trip
        # ended since this rider's last /chat turn) is appended the same
        # way -- also plain, deterministic text, never LLM-narrated.
        notification_texts = [row["pending_notification"] for row in claimed]
        if feedback_prompt is not None:
            notification_texts.append(FEEDBACK_REQUEST_TEXT)
        return "\n\n".join(notification_texts + [reply_text])
