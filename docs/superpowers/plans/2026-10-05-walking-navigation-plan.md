# Walking Navigation & Location Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Narrate real walking directions (to the first stop, between transfers, from the last stop) and let users specify a trip's origin/destination via current location or a typed address, not just a named transit stop.

**Architecture:** Extend `Leg`/`otp_client.py` with OTP's already-confirmed-live `steps`/`headsign`/lat-lon fields (pure data plumbing, all fields optional with safe defaults). Let the Conversation Agent's existing LLM-narration path condense raw walk-step JSON into prose (no new Python formatting layer — same pattern it already uses for route names/times). Add two new origin-input paths (geolocation, Geoclient address geocoding) that feed `plan_route` the same way a `find_stop` result already does. Extend the Re-plan Agent's existing template-only notification path with a small deterministic step-condenser (a lookup table over OTP's real enum values, not LLM narration — Re-plan is template-first by design).

**Tech Stack:** FastAPI/Pydantic (backend), React/TypeScript (frontend), Anthropic tool-use (Claude Haiku), OTP GraphQL, NYC Geoclient API v2.

Design doc: `docs/superpowers/specs/2026-09-10-walking-navigation-design.md` — read it in full before starting; it has the live-verified OTP schema facts (real enum values, confirmed-null platform fields, headsign research) this plan builds against without re-deriving them.

## Global Constraints

- **Never fabricate** (project-wide hard rule): a step with a null `street_name` is narrated as generic, never invented. Geocoding/geolocation failure asks the user, never guesses coordinates.
- **LLM agents never compute numbers, routes, or probabilities** — walk-leg narration is LLM prose over real OTP data (fine, matches existing route-name/time narration), but the Re-plan Agent's step-condenser is template-only Python, no LLM call (Re-plan is already template-first per spec §9.1's cost envelope).
- **Model choice is a config/env parameter, never hardcoded** — no new model usage in this plan; nothing here calls a new LLM path.
- **Do everything the minimal way** — no new UI surface for walking directions (chat-narrated text only, per spec §8/§9); no map/visual component.
- All new `Leg` fields are optional with safe defaults — zero changes to existing hand-constructed `Leg(...)` call sites across `test_risk_engine.py`, `test_deadline.py`, `test_conversation_agent.py`, `test_monitoring.py`, `test_replan_agent.py`, `test_trip_monitor.py`.

---

## Task 1: Extend `Leg` with steps/headsign/lat-lon, extend OTP client query

**Files:**
- Modify: `backend/app/models/transit.py`
- Modify: `backend/app/routing/otp_client.py`
- Test: `backend/tests/test_otp_client.py`

**Interfaces:**
- Produces: `WalkStep` model (`street_name: str | None`, `distance_meters: float`, `relative_direction: str | None`, `absolute_direction: str | None`, `exit: str | None`, `stay_on: bool = False`). `Leg` gains `headsign: str | None = None`, `steps: list[WalkStep] = []`, `from_lat: float | None = None`, `from_lon: float | None = None`, `to_lat: float | None = None`, `to_lon: float | None = None`. All later tasks that read a `Leg`'s new fields use these exact names.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_otp_client.py`, extending `MOCK_OTP_RESPONSE`'s one leg with the new fields and a new test:

```python
MOCK_OTP_RESPONSE_WITH_WALK_DATA = {
    "data": {
        "plan": {
            "itineraries": [
                {
                    "duration": 1800,
                    "legs": [
                        {
                            "mode": "WALK",
                            "route": None,
                            "from": {"name": "2 West Loop Rd", "stop": None, "lat": 40.7566, "lon": -73.9557},
                            "to": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06N"}, "lat": 40.7597, "lon": -73.9532},
                            "startTime": 1755100000000,
                            "endTime": 1755100500000,
                            "headsign": None,
                            "steps": [
                                {
                                    "streetName": "Main St",
                                    "distance": 120.5,
                                    "relativeDirection": "LEFT",
                                    "absoluteDirection": "NORTH",
                                    "exit": None,
                                    "stayOn": False,
                                }
                            ],
                        },
                        {
                            "mode": "SUBWAY",
                            "route": {"shortName": "F"},
                            "from": {"name": "Roosevelt Island", "stop": {"gtfsId": "MTA_NYCT_Subway:B06N"}, "lat": 40.7597, "lon": -73.9532},
                            "to": {"name": "Lexington Av/63 St", "stop": {"gtfsId": "MTA_NYCT_Subway:B08S"}, "lat": 40.7644, "lon": -73.9656},
                            "startTime": 1755100800000,
                            "endTime": 1755101400000,
                            "realTime": True,
                            "arrivalDelay": 90,
                            "headsign": "96 St",
                            "steps": [],
                        },
                    ],
                }
            ]
        }
    }
}


@pytest.mark.asyncio
async def test_plan_route_parses_walk_steps_headsign_and_latlon():
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_OTP_RESPONSE_WITH_WALK_DATA
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        itineraries = await client.plan_route(40.7566, -73.9557, 40.7644, -73.9656)

    walk_leg = itineraries[0].legs[0]
    assert walk_leg.from_lat == 40.7566
    assert walk_leg.from_lon == -73.9557
    assert walk_leg.to_lat == 40.7597
    assert walk_leg.to_lon == -73.9532
    assert walk_leg.headsign is None
    assert len(walk_leg.steps) == 1
    assert walk_leg.steps[0].street_name == "Main St"
    assert walk_leg.steps[0].distance_meters == 120.5
    assert walk_leg.steps[0].relative_direction == "LEFT"
    assert walk_leg.steps[0].absolute_direction == "NORTH"
    assert walk_leg.steps[0].stay_on is False

    subway_leg = itineraries[0].legs[1]
    assert subway_leg.headsign == "96 St"
    assert subway_leg.steps == []


@pytest.mark.asyncio
async def test_plan_route_defaults_new_fields_when_absent():
    """Old-shaped responses (no headsign/steps/lat-lon keys) must not break parsing."""
    client = OTPClient(base_url="http://localhost:8080")
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_OTP_RESPONSE  # the existing fixture, unchanged
    mock_response.raise_for_status = lambda: None
    with patch(
        "app.routing.otp_client.httpx.AsyncClient.post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        itineraries = await client.plan_route(40.7597, -73.9532, 40.7644, -73.9656)

    leg = itineraries[0].legs[0]
    assert leg.headsign is None
    assert leg.steps == []
    assert leg.from_lat is None
    assert leg.to_lon is None


def test_old_shaped_stored_itinerary_snapshot_still_round_trips():
    """A monitored_trips.itinerary_snapshot row written before this task
    (no steps/headsign/lat-lon keys in its stored JSON at all) must still
    deserialize correctly -- this is the actual regression the design doc
    calls for: an old *stored* snapshot round-tripping through
    model_validate_json, not a fresh OTP response being parsed."""
    old_shaped_json = (
        '{"duration_seconds": 1800, "legs": [{"mode": "SUBWAY", "agency": "MTA", '
        '"route_short_name": "F", "from_stop_id": "mtasbwy:B06N", "from_stop_name": "Roosevelt Island", '
        '"to_stop_id": "mtasbwy:B08S", "to_stop_name": "Lex/63 St", "start_time_ms": 1755100800000, '
        '"end_time_ms": 1755101400000, "real_time": true, "arrival_delay_seconds": 90}]}'
    )
    itinerary = Itinerary.model_validate_json(old_shaped_json)
    assert itinerary.legs[0].route_short_name == "F"
    assert itinerary.legs[0].headsign is None
    assert itinerary.legs[0].steps == []
    assert itinerary.legs[0].from_lat is None
```

Add `from app.models.transit import Itinerary` to this test file's imports if not already present (check first — `otp_client.py` already imports it, but the test file may only import `OTPClient`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_otp_client.py -v`
Expected: FAIL — `WalkStep` doesn't exist, `Leg` has no `headsign`/`steps`/lat-lon fields, and `MOCK_OTP_RESPONSE` (the old fixture, used in the second new test) has no `lat`/`lon` keys on `from`/`to` at all, so the existing-fixture test will pass trivially once the model change lands but the first new test will fail on `AttributeError`/`ValidationError`.

- [ ] **Step 3: Extend the models**

In `backend/app/models/transit.py`, replace the full file with:

```python
from pydantic import BaseModel


class WalkStep(BaseModel):
    street_name: str | None = None
    distance_meters: float
    relative_direction: str | None = None
    absolute_direction: str | None = None
    exit: str | None = None
    stay_on: bool = False


class Leg(BaseModel):
    mode: str
    agency: str = "MTA"
    route_short_name: str | None = None
    from_stop_id: str | None = None
    from_stop_name: str
    to_stop_id: str | None = None
    to_stop_name: str
    start_time_ms: int
    end_time_ms: int
    real_time: bool = False
    arrival_delay_seconds: int | None = None
    headsign: str | None = None
    steps: list[WalkStep] = []
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None


class Itinerary(BaseModel):
    duration_seconds: int
    legs: list[Leg]
```

- [ ] **Step 4: Extend the OTP client query and parsing**

In `backend/app/routing/otp_client.py`, replace the full file with:

```python
import httpx
from app.models.transit import Itinerary, Leg, WalkStep

PLAN_QUERY = """
query Plan($fromLat: Float!, $fromLon: Float!, $toLat: Float!, $toLon: Float!) {
  plan(from: {lat: $fromLat, lon: $fromLon}, to: {lat: $toLat, lon: $toLon}) {
    itineraries {
      duration
      legs {
        mode
        route { shortName }
        from { name stop { gtfsId } lat lon }
        to { name stop { gtfsId } lat lon }
        startTime
        endTime
        realTime
        arrivalDelay
        headsign
        steps { streetName distance relativeDirection absoluteDirection exit stayOn }
      }
    }
  }
}
"""


class OTPClient:
    def __init__(self, base_url: str):
        self._graphql_url = f"{base_url}/otp/routers/default/index/graphql"

    async def plan_route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> list[Itinerary]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._graphql_url,
                json={
                    "query": PLAN_QUERY,
                    "variables": {"fromLat": from_lat, "fromLon": from_lon, "toLat": to_lat, "toLon": to_lon},
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()["data"]["plan"]["itineraries"]

        return [
            Itinerary(
                duration_seconds=it["duration"],
                legs=[
                    Leg(
                        mode=leg["mode"],
                        route_short_name=(leg.get("route") or {}).get("shortName"),
                        from_stop_id=(leg["from"].get("stop") or {}).get("gtfsId"),
                        from_stop_name=leg["from"]["name"],
                        to_stop_id=(leg["to"].get("stop") or {}).get("gtfsId"),
                        to_stop_name=leg["to"]["name"],
                        start_time_ms=leg["startTime"],
                        end_time_ms=leg["endTime"],
                        real_time=leg.get("realTime", False),
                        arrival_delay_seconds=leg.get("arrivalDelay"),
                        headsign=leg.get("headsign"),
                        steps=[
                            WalkStep(
                                street_name=step.get("streetName"),
                                distance_meters=step["distance"],
                                relative_direction=step.get("relativeDirection"),
                                absolute_direction=step.get("absoluteDirection"),
                                exit=step.get("exit"),
                                stay_on=step.get("stayOn", False),
                            )
                            for step in (leg.get("steps") or [])
                        ],
                        from_lat=leg["from"].get("lat"),
                        from_lon=leg["from"].get("lon"),
                        to_lat=leg["to"].get("lat"),
                        to_lon=leg["to"].get("lon"),
                    )
                    for leg in it["legs"]
                ],
            )
            for it in data
        ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_otp_client.py -v`
Expected: PASS, 4/4 (2 pre-existing + 2 new).

- [ ] **Step 6: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass. This is the task that proves the "all new fields optional with safe defaults" claim — every pre-existing hand-constructed `Leg(...)` in the whole suite must still construct without changes.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/transit.py backend/app/routing/otp_client.py backend/tests/test_otp_client.py
git commit -m "Extend Leg with steps/headsign/lat-lon from OTP's live schema"
```

---

## Task 2: Primary chat narration — walk legs and headsign, SYSTEM_PROMPT only

**Files:**
- Modify: `backend/app/agents/conversation_agent.py`
- Test: `backend/tests/test_conversation_agent.py`

**Interfaces:**
- Consumes: `Leg.headsign`, `Leg.steps` (Task 1) — raw JSON already reaches the LLM via the existing `plan_route` tool-result dispatch (`content = json.dumps([it.model_dump() for it in itineraries])` in `respond()`), no code change needed there.
- Produces: no new function — `SYSTEM_PROMPT` text only.

**Key decisions (from the design doc — implement exactly):** No new Python formatting layer. The Conversation Agent already narrates raw `plan_route` JSON into prose for every other leg detail; `steps`/`headsign` ride the same raw JSON. Add three `SYSTEM_PROMPT` instructions: (1) condense walk legs into 1-2 natural sentences using real street names/distances/turns, collapsing trivial micro-segments; (2) never invent a street name OTP didn't return — a generic value like `"sidewalk"` is narrated as generic; (3) at every boarding and transfer, state "board the {route} toward **{headsign}**" with the headsign bolded (markdown), using the real headsign text, universally (not gated on `get_risk` having a citable number — this is navigation, not a risk statement).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_conversation_agent.py` (find this file's existing pattern for a `plan_route` dispatch test and match its mocking style — it already mocks `self._otp.plan_route` and inspects what `respond()` sends back to the LLM as a tool result):

```python
async def test_plan_route_tool_result_carries_real_headsign_and_steps(monkeypatch):
    # Mirrors this file's existing plan_route-dispatch test pattern: can't
    # test LLM prose quality, CAN test the data reaching the LLM is real
    # and present -- same honesty caveat as every other prompt-driven test
    # in this codebase.
    agent = ConversationAgent()
    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="WALK", from_stop_name="2 West Loop Rd", to_stop_name="Roosevelt Island",
                start_time_ms=0, end_time_ms=300000,
                steps=[WalkStep(street_name="Main St", distance_meters=120.5, relative_direction="LEFT")],
            ),
            Leg(
                mode="SUBWAY", route_short_name="F", from_stop_name="Roosevelt Island", to_stop_name="Lex/63 St",
                start_time_ms=300000, end_time_ms=900000, headsign="96 St",
            ),
        ],
    )
    monkeypatch.setattr(agent._otp, "plan_route", AsyncMock(return_value=[itinerary]))

    captured_messages = []

    async def fake_create(messages):
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _tool_use_response("plan_route", {"from_lat": 0, "from_lon": 0, "to_lat": 0, "to_lon": 0})
        return _text_response("Walk down Main St, then board the F toward 96 St.")

    monkeypatch.setattr(agent, "_create", fake_create)

    await agent.respond("how do I get to Lex/63", [], "anon-1")

    tool_result_content = captured_messages[1][-1]["content"][0]["content"]
    assert "Main St" in tool_result_content
    assert "96 St" in tool_result_content
```

This test needs `_tool_use_response`/`_text_response` helpers and `AsyncMock` imports — check `test_conversation_agent.py`'s existing imports/helpers first (it almost certainly already has equivalents for its existing `plan_route` test, given this exact mocking shape is already established there); reuse them by their existing names rather than redefining. If no such helper exists yet, add minimal ones matching this shape:

```python
def _tool_use_response(tool_name, tool_input):
    block = MagicMock(type="tool_use", name=tool_name, input=tool_input, id="tu_1")
    return MagicMock(stop_reason="tool_use", content=[block])


def _text_response(text):
    block = MagicMock(type="text", text=text)
    return MagicMock(stop_reason="end_turn", content=[block])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py::test_plan_route_tool_result_carries_real_headsign_and_steps -v`
Expected: this specific assertion should actually PASS already once Task 1 lands, since `respond()` already serializes the full `Leg` (including the new fields) via `model_dump()` with zero code change needed — the raw JSON dispatch is already generic. If it does pass immediately, this is a legitimate "proves no production code change was needed" result (matching this project's own precedent from the ferry-routing plan's Task 4) — do not add code to make it pass artificially. Report this clearly rather than silently treating it as a normal RED step.

- [ ] **Step 3: Update `SYSTEM_PROMPT`**

In `backend/app/agents/conversation_agent.py`, insert this new paragraph into `SYSTEM_PROMPT` (after the existing `"Keep answers to 1-3 sentences.\n\n"` line, before the `"Citation format:"` paragraph):

```python
    "Keep answers to 1-3 sentences.\n\n"
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
```

(Only the two new sentences between "Keep answers..." and "Citation format:" are new — everything else here is existing text shown for exact insertion-point context.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py -v`
Expected: PASS, including the new test.

- [ ] **Step 5: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agents/conversation_agent.py backend/tests/test_conversation_agent.py
git commit -m "Narrate walk-leg steps and bolded headsigns in chat replies"
```

---

## Task 3: `user_location` — backend plumbing

**Files:**
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/agents/conversation_agent.py`
- Test: `backend/tests/test_conversation_agent.py`

**Interfaces:**
- Produces: `ChatRequest.user_location: dict | None = None` (shape `{"lat": float, "lon": float}`). `ConversationAgent.respond(self, user_message, conversation_history, anonymous_id, user_location=None)` — a new optional 4th parameter, defaulting to `None` so Task 2's test and every other existing caller of `respond()` needs no change.

**Key decisions (from the design doc — implement exactly):** `user_location`, when present, is injected into the per-call user message only (`[User's current location: lat, lon]`), never into the cached `SYSTEM_PROMPT`/`_SYSTEM_BLOCKS` — the exact same mechanism already used for `[Current time: ...]`. `SYSTEM_PROMPT` gets one new instruction: when the user references their current location and `user_location` is present, use those coordinates directly for `plan_route` — never call `find_stop` for this case; if `user_location` is absent, ask the user to share their location or name a place instead.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_conversation_agent.py`:

```python
async def test_user_location_injected_into_user_message_not_system_prompt(monkeypatch):
    agent = ConversationAgent()
    captured_messages = []

    async def fake_create(messages):
        captured_messages.append(messages)
        return _text_response("You're near Roosevelt Island.")

    monkeypatch.setattr(agent, "_create", fake_create)

    await agent.respond("where am I", [], "anon-1", user_location={"lat": 40.7597, "lon": -73.9532})

    sent_user_message = captured_messages[0][-1]["content"]
    assert "40.7597" in sent_user_message
    assert "-73.9532" in sent_user_message
    # Never in the cached system block -- that must stay byte-identical
    # across calls regardless of user_location, or prompt caching breaks.
    assert "40.7597" not in SYSTEM_PROMPT


async def test_no_user_location_key_when_not_given(monkeypatch):
    agent = ConversationAgent()
    captured_messages = []

    async def fake_create(messages):
        captured_messages.append(messages)
        return _text_response("Sure, where are you headed from?")

    monkeypatch.setattr(agent, "_create", fake_create)

    await agent.respond("plan a trip", [], "anon-1")

    sent_user_message = captured_messages[0][-1]["content"]
    assert "location" not in sent_user_message.lower()
```

(Import `SYSTEM_PROMPT` at the top of the test file if not already imported.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py::test_user_location_injected_into_user_message_not_system_prompt tests/test_conversation_agent.py::test_no_user_location_key_when_not_given -v`
Expected: FAIL — `respond()` doesn't accept `user_location` yet (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Update `respond()` and `ChatRequest`**

In `backend/app/agents/conversation_agent.py`, change the `respond()` signature and message-building:

```python
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
        now_iso = datetime.now(timezone.utc).isoformat()
        prefix = f"[Current time: {now_iso}]"
        if user_location is not None:
            prefix += f"\n[User's current location: {user_location['lat']}, {user_location['lon']}]"
        messages = conversation_history + [
            {"role": "user", "content": f"{prefix}\n{user_message}"}
        ]
```

In `backend/app/api/chat.py`, add the field and pass it through:

```python
class ChatRequest(BaseModel):
    message: str
    conversation_history: list[ChatMessage]
    anonymous_id: str  # passed through to respond() for monitored-trip tool dispatch (Task 5);
    # Task 8 additionally uses it inside respond() to claim + prepend pending notifications
    user_location: dict | None = None  # {"lat": float, "lon": float} or None -- walking-nav design doc §2


@router.post("/chat")
async def chat(req: ChatRequest):
    agent = ConversationAgent()
    history = [{"role": m.role, "content": m.content} for m in req.conversation_history]
    reply = await agent.respond(req.message, history, req.anonymous_id, req.user_location)
    return {"reply": reply}
```

- [ ] **Step 4: Update `SYSTEM_PROMPT`**

Insert this new sentence into `SYSTEM_PROMPT`, in the existing paragraph that already covers `find_stop` (the one starting `"If the user names a place instead of giving coordinates..."`):

```python
    "If the user names a place instead of giving coordinates, call "
    "find_stop first to resolve it, then use the result's lat/lon with "
    "plan_route — never invent coordinates. If the user refers to their "
    "current location (e.g. 'from here', 'near me') and a "
    "[User's current location: ...] marker is present on the message, use "
    "those coordinates directly with plan_route — never call find_stop for "
    "this case. If they reference their current location but no marker is "
    "present, ask them to share their location or name a place instead. "
    "Keep answers to 1-3 sentences.\n\n"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py -v`
Expected: PASS, all tests including the two new ones.

- [ ] **Step 6: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/chat.py backend/app/agents/conversation_agent.py backend/tests/test_conversation_agent.py
git commit -m "Add user_location to /chat, injected per-call like current time"
```

---

## Task 4: Geolocation — frontend

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/api/client.test.ts`

**Interfaces:**
- Produces: `sendChatMessage(message, conversationHistory, userLocation?: {lat: number, lon: number} | null)` — `userLocation` is a new optional 3rd parameter.
- Produces: `shouldRequestLocation(message: string): boolean` — the keyword heuristic, exported for direct unit testing.

**Key decisions (from the design doc — implement exactly):** No proactive permission prompt on page load. A cheap client-side keyword check (case-insensitive substring match against `["here", "my location", "near me", "current location", "where i am"]`) on the outgoing message text triggers `navigator.geolocation.getCurrentPosition()` at send time, the *first* time it matches in a session — a real user gesture. Once granted, coordinates are cached in memory (module-level, not React state — it must survive across renders without triggering re-renders) and attached to every subsequent `/chat` request for the rest of the session, re-requesting only if the cached value is older than 5 minutes.

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/api/client.test.ts` (check this file's existing `fetch`-mocking pattern for `sendChatMessage`/`anonymous_id` first and match it exactly):

```typescript
describe('shouldRequestLocation', () => {
  it('matches known location phrases case-insensitively', () => {
    expect(shouldRequestLocation('get me home from here')).toBe(true)
    expect(shouldRequestLocation('What is NEAR ME right now')).toBe(true)
    expect(shouldRequestLocation('take me to my location please')).toBe(false) // "my location" as typed below
    expect(shouldRequestLocation('take me home from my location')).toBe(true)
    expect(shouldRequestLocation('plan a trip to Roosevelt Island')).toBe(false)
  })
})

describe('sendChatMessage with location', () => {
  const mockGeolocation = {
    getCurrentPosition: vi.fn(),
  }

  beforeEach(() => {
    vi.stubGlobal('navigator', { geolocation: mockGeolocation })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'ok' }),
    }))
  })

  it('attaches user_location after a successful geolocation request triggered by a keyword match', async () => {
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      success({ coords: { latitude: 40.7597, longitude: -73.9532 } })
    })

    await sendChatMessage('get me home from here', [])

    const callBody = JSON.parse((fetch as any).mock.calls[0][1].body)
    expect(callBody.user_location).toEqual({ lat: 40.7597, lon: -73.9532 })
  })

  it('sends user_location: null when the message has no location keyword and none was cached yet', async () => {
    await sendChatMessage('plan a trip to Roosevelt Island', [])

    const callBody = JSON.parse((fetch as any).mock.calls[0][1].body)
    expect(callBody.user_location).toBeNull()
    expect(mockGeolocation.getCurrentPosition).not.toHaveBeenCalled()
  })

  it('re-requests geolocation when the cached value is older than 5 minutes', async () => {
    let callCount = 0
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      callCount += 1
      success({ coords: { latitude: 40.0 + callCount, longitude: -73.0 } })
    })
    vi.useFakeTimers()

    await sendChatMessage('get me home from here', [])
    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(6 * 60 * 1000) // 6 minutes -- past the 5-minute staleness window

    await sendChatMessage('get me home from here', [])
    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(2)

    const secondCallBody = JSON.parse((fetch as any).mock.calls[1][1].body)
    expect(secondCallBody.user_location).toEqual({ lat: 42.0, lon: -73.0 }) // the re-requested value, not the stale cached one

    vi.useRealTimers()
  })

  it('does not re-request geolocation within the 5-minute window', async () => {
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      success({ coords: { latitude: 40.7597, longitude: -73.9532 } })
    })
    vi.useFakeTimers()

    await sendChatMessage('get me home from here', [])
    vi.advanceTimersByTime(2 * 60 * 1000) // 2 minutes -- still within the window
    await sendChatMessage('take me from here to work', [])

    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(1) // not called again

    vi.useRealTimers()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- client.test.ts`
Expected: FAIL — `shouldRequestLocation` isn't exported, `sendChatMessage` doesn't request geolocation or send `user_location`.

- [ ] **Step 3: Implement in `client.ts`**

Replace the full file:

```typescript
export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatResponse {
  reply: string
}

export interface UserLocation {
  lat: number
  lon: number
}

const ANONYMOUS_ID_KEY = 'imts_anonymous_id'
const LOCATION_PHRASES = ['here', 'my location', 'near me', 'current location', 'where i am']
const LOCATION_STALE_AFTER_MS = 5 * 60 * 1000

let cachedLocation: UserLocation | null = null
let cachedLocationAt: number | null = null

export function getOrCreateAnonymousId(): string {
  try {
    const existing = localStorage.getItem(ANONYMOUS_ID_KEY)
    if (existing) return existing
    const id = crypto.randomUUID()
    localStorage.setItem(ANONYMOUS_ID_KEY, id)
    return id
  } catch {
    // localStorage unavailable (e.g. private browsing, storage disabled) --
    // degrade to a per-session id rather than crashing the send flow.
    // The user simply won't get cross-session monitoring continuity.
    return crypto.randomUUID()
  }
}

export function shouldRequestLocation(message: string): boolean {
  const lower = message.toLowerCase()
  return LOCATION_PHRASES.some((phrase) => lower.includes(phrase))
}

function isLocationStale(): boolean {
  return cachedLocationAt === null || Date.now() - cachedLocationAt > LOCATION_STALE_AFTER_MS
}

function requestLocation(): Promise<UserLocation | null> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) {
      resolve(null)
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) => resolve({ lat: position.coords.latitude, lon: position.coords.longitude }),
      () => resolve(null) // denied/unavailable -- design doc: no user_location sent, agent asks instead
    )
  })
}

export async function sendChatMessage(
  message: string,
  conversationHistory: ChatMessage[]
): Promise<ChatResponse> {
  if (shouldRequestLocation(message) && isLocationStale()) {
    const result = await requestLocation()
    if (result !== null) {
      cachedLocation = result
      cachedLocationAt = Date.now()
    }
  }

  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      conversation_history: conversationHistory,
      anonymous_id: getOrCreateAnonymousId(),
      user_location: isLocationStale() ? null : cachedLocation,
    }),
  })
  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }
  return response.json()
}
```

Note: `"take me to my location please"` does NOT match `"my location"` as a substring the way the test above implies at first read — re-check that test case before trusting it; `"take me to my location please"` DOES contain the literal substring `"my location"`, so it should match `true`, not `false`. Fix the test's expected value to `true` for that line before running it (this is a planning-time correction, not a runtime ambiguity — apply it now): `expect(shouldRequestLocation('take me to my location please')).toBe(true)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- client.test.ts`
Expected: PASS, all tests.

- [ ] **Step 5: Wire into `App.tsx`**

`App.tsx`'s `sendMessage()` already calls `sendChatMessage(trimmed, history)` — no change needed there, since `sendChatMessage` itself now handles geolocation internally. Confirm by reading the current `sendMessage()` body (around line 51-70) that it still only needs the two existing arguments; if so, this task needs no `App.tsx` change at all. If a review finds it does need a change (e.g. to surface a "getting your location..." loading state), that's a scope decision for the task reviewer to flag, not something to add speculatively here.

- [ ] **Step 6: Run the full frontend test suite**

Run: `cd frontend && npm test`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/api/client.test.ts
git commit -m "Attach geolocation to chat requests on a location-phrase match"
```

---

## Task 5: Address geocoding — `find_address` tool (NYC Geoclient)

**Files:**
- Modify: `backend/app/config.py`
- Create: `backend/app/geocode.py`
- Modify: `backend/app/agents/tools.py`
- Modify: `backend/app/agents/conversation_agent.py`
- Test: `backend/tests/test_geocode.py`
- Test: `backend/tests/test_conversation_agent.py`

**Interfaces:**
- Produces: `geocode_address(address: str) -> dict | None` in `backend/app/geocode.py` — returns `{"lat": float, "lon": float}` on success, `None` on no-match/failure. `FIND_ADDRESS_TOOL` dict in `tools.py`, matching `FIND_STOP_TOOL`'s shape. `ConversationAgent._handle_find_address` or inline dispatch in `respond()`'s tool-use loop.

**Key decisions (from the design doc — implement exactly):** `GET https://api.nyc.gov/geoclient/v2/search.json?input={url-encoded address}&subscription-key={key}`. Success shape: `results[0].response.{latitude, longitude}` (real WGS84 floats — NOT `xCoordinate`/`yCoordinate`). No-match shape: `status: "REJECTED"`, `results: []`. Scoped to NYC only (no bounding-box check needed — the geocoder itself is NYC-scoped).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_geocode.py`:

```python
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.geocode import geocode_address

EXACT_MATCH_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "status": "EXACT_MATCH",
            "response": {
                "latitude": 40.756552,
                "longitude": -73.955881,
                "firstBoroughName": "MANHATTAN",
                "xCoordinate": "999999",  # real field present in the live response, must NOT be used
                "yCoordinate": "888888",
            },
        }
    ],
}

REJECTED_RESPONSE = {"status": "REJECTED", "results": []}


@pytest.mark.asyncio
async def test_geocode_address_parses_exact_match():
    mock_response = AsyncMock()
    mock_response.json = lambda: EXACT_MATCH_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        result = await geocode_address("2 west loop road manhattan")

    assert result == {"lat": 40.756552, "lon": -73.955881}
    call_kwargs = mock_get.await_args.kwargs
    assert call_kwargs["params"]["input"] == "2 west loop road manhattan"


@pytest.mark.asyncio
async def test_geocode_address_returns_none_on_rejected():
    mock_response = AsyncMock()
    mock_response.json = lambda: REJECTED_RESPONSE
    mock_response.raise_for_status = lambda: None
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await geocode_address("roosevelt island")  # colloquial, real no-match per design doc

    assert result is None


@pytest.mark.asyncio
async def test_geocode_address_returns_none_on_http_error():
    with patch("app.geocode.httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")):
        result = await geocode_address("anything")

    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_geocode.py -v`
Expected: FAIL — `app.geocode` doesn't exist.

- [ ] **Step 3: Add the config field**

In `backend/app/config.py`, add inside `Settings`:

```python
    geoclient_api_key: str = ""
```

- [ ] **Step 4: Write `backend/app/geocode.py`**

```python
"""backend/app/geocode.py

geocode_address(address) -- NYC Geoclient API v2 address lookup, for the
walking-navigation design's third location-input method (named stop via
find_stop, current location via geolocation, typed address via this).
Scoped to NYC only: this product's routing coverage (OTP's graph, GTFS
feeds) is NYC-only regardless, and Geoclient itself is NYC-scoped, so no
extra bounding-box/plausibility check is needed beyond what the API
already guarantees.

Real response shape confirmed live (see docs/superpowers/specs/
2026-09-10-walking-navigation-design.md): success is
results[0].response.{latitude, longitude} as real WGS84 floats -- NOT
the also-present xCoordinate/yCoordinate NY State Plane fields. No-match
is status: "REJECTED", results: []. Never raises for expected failure
modes (no match, HTTP error) -- geocode_address returns None and the
caller (find_address tool dispatch) surfaces an honest "couldn't find
that address" rather than guessing.
"""
import httpx

from app.config import settings

GEOCLIENT_URL = "https://api.nyc.gov/geoclient/v2/search.json"


async def geocode_address(address: str) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GEOCLIENT_URL,
                params={"input": address, "subscription-key": settings.geoclient_api_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError:
        return None

    results = data.get("results") or []
    if not results:
        return None

    geo_response = results[0].get("response") or {}
    lat = geo_response.get("latitude")
    lon = geo_response.get("longitude")
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_geocode.py -v`
Expected: PASS, 3/3.

- [ ] **Step 6: Add `FIND_ADDRESS_TOOL` and wire dispatch**

In `backend/app/agents/tools.py`, add after `FIND_STOP_TOOL`:

```python
FIND_ADDRESS_TOOL = {
    "name": "find_address",
    "description": "Look up a typed street address within NYC to get its coordinates (e.g. '2 West Loop Road, Manhattan'). Include a real NYC borough name if you know one — a neighborhood nickname (e.g. 'Roosevelt Island') may not match as well as a real borough name. Call this when the user types an address instead of naming a transit stop or giving their current location, then use the result's lat/lon with plan_route. If it returns no match, ask the user to clarify or try a stop name instead — never guess coordinates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "The free-text address to geocode, ideally including a real NYC borough name."},
        },
        "required": ["address"],
    },
}
```

In `backend/app/agents/conversation_agent.py`: add the import, add `FIND_ADDRESS_TOOL` to whatever list of tools is passed to the Anthropic API call (find the existing list literal — it's built from `PLAN_ROUTE_TOOL`, `FIND_STOP_TOOL`, `GET_RISK_TOOL`, etc., check `_create`'s or `__init__`'s body for the exact variable name), and add a new `elif` branch in `respond()`'s tool-use dispatch loop:

```python
                elif tool_use.name == "find_address":
                    result = await geocode_address(tool_use.input["address"])
                    content = json.dumps(result)
```

Add `from app.geocode import geocode_address` to the imports at the top of `conversation_agent.py`, and `FIND_ADDRESS_TOOL` to the existing `from app.agents.tools import (...)` block.

Add one sentence to `SYSTEM_PROMPT`, in the same paragraph extended in Task 3 (right after the `find_stop`/current-location sentences, before `"Keep answers..."`):

```python
    "If the user types a street address, call find_address to resolve it, "
    "then use the result's lat/lon with plan_route — if it returns null, "
    "tell them you couldn't find that address and ask them to clarify or "
    "try a stop name instead, never invent coordinates. "
    "Keep answers to 1-3 sentences.\n\n"
```

- [ ] **Step 7: Write the dispatch test**

Add to `backend/tests/test_conversation_agent.py`:

```python
async def test_find_address_tool_dispatches_to_geocode_address(monkeypatch):
    agent = ConversationAgent()
    captured_messages = []

    async def fake_create(messages):
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _tool_use_response("find_address", {"address": "2 west loop road manhattan"})
        return _text_response("Found it.")

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(
        "app.agents.conversation_agent.geocode_address",
        AsyncMock(return_value={"lat": 40.756552, "lon": -73.955881}),
    )

    await agent.respond("plan from 2 west loop road manhattan", [], "anon-1")

    tool_result_content = captured_messages[1][-1]["content"][0]["content"]
    assert "40.756552" in tool_result_content
    assert "-73.955881" in tool_result_content


async def test_find_address_tool_passes_through_none_on_no_match(monkeypatch):
    agent = ConversationAgent()
    captured_messages = []

    async def fake_create(messages):
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _tool_use_response("find_address", {"address": "roosevelt island"})
        return _text_response("Couldn't find that — try a stop name?")

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr("app.agents.conversation_agent.geocode_address", AsyncMock(return_value=None))

    await agent.respond("plan from roosevelt island the neighborhood", [], "anon-1")

    tool_result_content = captured_messages[1][-1]["content"][0]["content"]
    assert tool_result_content == "null"
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py tests/test_geocode.py -v`
Expected: PASS, all tests.

- [ ] **Step 9: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add backend/app/config.py backend/app/geocode.py backend/app/agents/tools.py backend/app/agents/conversation_agent.py backend/tests/test_geocode.py backend/tests/test_conversation_agent.py
git commit -m "Add find_address tool backed by NYC Geoclient geocoding"
```

---

## Task 6: Mid-trip destination change — SYSTEM_PROMPT orchestration, zero new backend code

**Files:**
- Modify: `backend/app/agents/conversation_agent.py`
- Test: `backend/tests/test_conversation_agent.py`

**Interfaces:**
- Consumes: `cancel_monitored_trip` (existing, already auto-resolves with no `trip_id` — see `_handle_cancel_monitored_trip`), `find_stop`/`find_address` (Task 5), `plan_route`, `user_location` (Task 3). No new tool, no new Python dispatch logic.

**Key decisions (from the design doc — implement exactly):** When the user changes their destination mid-conversation, `SYSTEM_PROMPT` tells the agent to: (1) call `cancel_monitored_trip` with no `trip_id`; (2) if `user_location` isn't already available this turn, ask the user for their current location; (3) call `plan_route` fresh from that location to the newly-stated destination; (4) fall through to the existing proactive-monitoring-offer logic. This task is a **regression/proof task first, prompt change second** — matching this project's own precedent (ferry-routing's Task 4): write the test, run it against the CURRENT `SYSTEM_PROMPT` first to see whether it already happens to pass, and only add the prompt text if it doesn't.

- [ ] **Step 1: Write the test**

Add to `backend/tests/test_conversation_agent.py`:

```python
async def test_destination_change_cancels_trip_before_replanning(monkeypatch):
    agent = ConversationAgent()
    captured_messages = []
    tool_call_order = []

    async def fake_create(messages):
        captured_messages.append(messages)
        if len(captured_messages) == 1:
            return _tool_use_response("cancel_monitored_trip", {})
        if len(captured_messages) == 2:
            return _tool_use_response("plan_route", {"from_lat": 40.76, "from_lon": -73.95, "to_lat": 40.77, "to_lon": -73.96})
        return _text_response("Okay, rerouting you.")

    def fake_cancel(tool_input, anonymous_id):
        tool_call_order.append("cancel_monitored_trip")
        return json.dumps({"cancelled": True, "trip_id": 1})

    monkeypatch.setattr(agent, "_create", fake_create)
    monkeypatch.setattr(agent, "_handle_cancel_monitored_trip", fake_cancel)
    monkeypatch.setattr(agent._otp, "plan_route", AsyncMock(return_value=[]))

    await agent.respond(
        "actually take me to Lex/63 instead", [], "anon-1",
        user_location={"lat": 40.76, "lon": -73.95},
    )

    assert tool_call_order == ["cancel_monitored_trip"]
```

- [ ] **Step 2: Run test to see if it already passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py::test_destination_change_cancels_trip_before_replanning -v`

If it FAILS: this test only checks that `cancel_monitored_trip` dispatch works when the LLM calls it first — which is purely a mocking/sequencing issue in the test itself if it fails, since this codebase's actual Global Constraint is "LLM agents never compute numbers/routes" — the LLM decides *when* to call `cancel_monitored_trip`, a real model call in production. This specific test can only verify the tool-dispatch plumbing supports the sequence, not that the model reliably chooses it — proceed to Step 3 regardless to add the prompt instruction (the actual behavioral correctness lives in the prompt text, not in this test).

- [ ] **Step 3: Add the `SYSTEM_PROMPT` instruction**

Add this new paragraph to `SYSTEM_PROMPT`, after the existing `cancel_monitored_trip`/trip-disambiguation paragraph (the one ending `"...never cancel more than one trip for one request."`):

```python
    "cancel_monitored_trip again with the trip_id they pick. Never guess which\n"
    "trip they mean and never cancel more than one trip for one request.\n\n"
    "Mid-trip destination change: if the user changes their destination while "
    "a trip is active (e.g. 'actually take me to X instead'), first call "
    "cancel_monitored_trip with no trip_id (it auto-resolves their active "
    "trip), then get their current location the same way as any other "
    "current-location reference (use it if already present on the message, "
    "otherwise ask for it), then call plan_route fresh from that location to "
    "the newly-named destination (resolving it via find_stop/find_address "
    "same as any new trip request), then fall through to this prompt's "
    "normal monitoring-offer logic for the new trip."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_conversation_agent.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agents/conversation_agent.py backend/tests/test_conversation_agent.py
git commit -m "Add mid-trip destination change orchestration to SYSTEM_PROMPT"
```

---

## Task 7: Re-plan notification enrichment — walking directions in `replan_agent.py`

**Files:**
- Modify: `backend/app/agents/replan_agent.py`
- Test: `backend/tests/test_replan_agent.py`

**Interfaces:**
- Produces: `condense_walk_steps(steps: list[WalkStep]) -> str` in `replan_agent.py` — deterministic, template-only (no LLM), the Re-plan Agent's own equivalent of the Conversation Agent's LLM-driven walk narration (Task 2), since Re-plan is template-first by design (spec §9.1).

**Key decisions (from the design doc — implement exactly):** A complete lookup table mapping all 16 confirmed-live `RelativeDirection` enum values to fixed phrases (`LEFT`→"turn left", `RIGHT`→"turn right", `SLIGHTLY_LEFT`→"bear left", `SLIGHTLY_RIGHT`→"bear right", `HARD_LEFT`→"turn sharply left", `HARD_RIGHT`→"turn sharply right", `CONTINUE`→"continue straight", `DEPART`→"head {absolute_direction}", `ENTER_STATION`→"enter the station", `EXIT_STATION`→"exit the station", `FOLLOW_SIGNS`→"follow the signs", `UTURN_LEFT`→"make a U-turn", `UTURN_RIGHT`→"make a U-turn", `CIRCLE_CLOCKWISE`→"circle around", `CIRCLE_COUNTERCLOCKWISE`→"circle around", `ELEVATOR`→"take the elevator"). Consecutive steps sharing the same `relative_direction` merge into one phrase. `replan_trip`'s destination resolution changes from always-`StopIndex`-based to `trip.itinerary_snapshot.legs[-1].to_lat`/`to_lon` directly when present, falling back to the current `StopIndex` path only when absent (an old stored trip predating Task 1).

- [ ] **Step 1: Write the failing test for `condense_walk_steps`**

Add to `backend/tests/test_replan_agent.py`:

```python
from app.agents.replan_agent import condense_walk_steps
from app.models.transit import WalkStep


def test_condense_walk_steps_merges_consecutive_same_direction():
    steps = [
        WalkStep(street_name="Main St", distance_meters=50, relative_direction="LEFT"),
        WalkStep(street_name="Main St", distance_meters=30, relative_direction="LEFT"),
        WalkStep(street_name="Broadway", distance_meters=100, relative_direction="RIGHT"),
    ]
    result = condense_walk_steps(steps)
    assert result == "Turn left on Main St, then turn right on Broadway."


def test_condense_walk_steps_covers_every_real_enum_value():
    enum_to_expected_phrase = {
        "CIRCLE_CLOCKWISE": "circle around",
        "CIRCLE_COUNTERCLOCKWISE": "circle around",
        "CONTINUE": "continue straight",
        "DEPART": "head north",
        "ELEVATOR": "take the elevator",
        "ENTER_STATION": "enter the station",
        "EXIT_STATION": "exit the station",
        "FOLLOW_SIGNS": "follow the signs",
        "HARD_LEFT": "turn sharply left",
        "HARD_RIGHT": "turn sharply right",
        "LEFT": "turn left",
        "RIGHT": "turn right",
        "SLIGHTLY_LEFT": "bear left",
        "SLIGHTLY_RIGHT": "bear right",
        "UTURN_LEFT": "make a U-turn",
        "UTURN_RIGHT": "make a U-turn",
    }
    for enum_value, phrase in enum_to_expected_phrase.items():
        step = WalkStep(street_name="Test St", distance_meters=10, relative_direction=enum_value, absolute_direction="NORTH")
        result = condense_walk_steps([step])
        assert phrase in result.lower(), f"{enum_value} -> expected phrase fragment {phrase!r} in {result!r}"


def test_condense_walk_steps_handles_null_street_name():
    steps = [WalkStep(street_name=None, distance_meters=20, relative_direction="CONTINUE")]
    result = condense_walk_steps(steps)
    assert "continue straight" in result.lower()
    assert "None" not in result


def test_condense_walk_steps_empty_list_returns_empty_string():
    assert condense_walk_steps([]) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_replan_agent.py -k condense_walk_steps -v`
Expected: FAIL — `condense_walk_steps` doesn't exist.

- [ ] **Step 3: Implement `condense_walk_steps`**

In `backend/app/agents/replan_agent.py`, add near the top (after the existing imports, before `_route_signature`):

```python
from app.models.transit import WalkStep

_DIRECTION_PHRASES = {
    "CIRCLE_CLOCKWISE": "circle around",
    "CIRCLE_COUNTERCLOCKWISE": "circle around",
    "CONTINUE": "continue straight",
    "ELEVATOR": "take the elevator",
    "ENTER_STATION": "enter the station",
    "EXIT_STATION": "exit the station",
    "FOLLOW_SIGNS": "follow the signs",
    "HARD_LEFT": "turn sharply left",
    "HARD_RIGHT": "turn sharply right",
    "LEFT": "turn left",
    "RIGHT": "turn right",
    "SLIGHTLY_LEFT": "bear left",
    "SLIGHTLY_RIGHT": "bear right",
    "UTURN_LEFT": "make a U-turn",
    "UTURN_RIGHT": "make a U-turn",
}


def _phrase_for_step(step: WalkStep) -> str:
    if step.relative_direction == "DEPART":
        direction = (step.absolute_direction or "").lower() or "the route"
        phrase = f"head {direction}"
    else:
        phrase = _DIRECTION_PHRASES.get(step.relative_direction or "", "continue")
    if step.street_name:
        return f"{phrase} on {step.street_name}"
    return phrase


def condense_walk_steps(steps: list[WalkStep]) -> str:
    """Deterministic, template-only walk-step condenser for the Re-plan
    Agent's notification text -- the Re-plan Agent is template-first by
    design (spec §9.1's cost envelope), so this is a lookup table over
    OTP's real confirmed-live enum values, not LLM narration (contrast
    Task 2's Conversation Agent path, which DOES use LLM prose since the
    primary chat path already does that for every other leg detail)."""
    if not steps:
        return ""
    merged: list[str] = []
    prev_direction = None
    for step in steps:
        if step.relative_direction == prev_direction and merged:
            continue  # consecutive same-direction steps collapse into the first phrase
        merged.append(_phrase_for_step(step))
        prev_direction = step.relative_direction
    sentence = ", then ".join(merged)
    return sentence[0].upper() + sentence[1:] + "."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_replan_agent.py -k condense_walk_steps -v`
Expected: PASS, 4/4.

- [ ] **Step 5: Write the failing test for destination resolution using `to_lat`/`to_lon`**

Add to `backend/tests/test_replan_agent.py` (this file's existing fixtures `conn`, `route_index`, `stop_index`, `patch_indexes` from its top section already apply):

```python
async def test_replan_uses_true_destination_lat_lon_when_present(conn, monkeypatch):
    trip = create_monitored_trip(
        conn, "anon-1",
        Itinerary(duration_seconds=900, legs=[
            _subway_leg("F", "A1", "A2", "Roosevelt Island", "Transfer Stop", _local_ms(2026, 9, 1, 8), _local_ms(2026, 9, 1, 8, 20)),
        ]),
        deadline_ts=None,
    )
    # Give the stored itinerary's final leg a real to_lat/to_lon, distinct
    # from stop_index's "Transfer Stop" coordinates -- proves the new
    # resolution path is actually used, not silently falling back.
    stored = MonitoredTrip.model_validate(
        {**trip.model_dump(), "itinerary_snapshot": Itinerary(duration_seconds=900, legs=[
            Leg(
                mode="SUBWAY", route_short_name="F", from_stop_id="mtasbwy:A1", from_stop_name="Roosevelt Island",
                to_stop_id="mtasbwy:A2", to_stop_name="Transfer Stop",
                start_time_ms=_local_ms(2026, 9, 1, 8), end_time_ms=_local_ms(2026, 9, 1, 8, 20),
                to_lat=40.9999, to_lon=-73.9999,
            ),
        ])}
    )

    plan_route_mock = AsyncMock(return_value=[Itinerary(duration_seconds=600, legs=[
        _subway_leg("Q", "A1", "A2", "Roosevelt Island", "Transfer Stop", _local_ms(2026, 9, 1, 8), _local_ms(2026, 9, 1, 8, 10)),
    ])])
    monkeypatch.setattr("app.agents.replan_agent.OTPClient.plan_route", plan_route_mock)

    await replan_trip(stored, "test", conn=conn)

    call_args = plan_route_mock.await_args.args
    assert call_args[2] == 40.9999  # to_lat positional arg
    assert call_args[3] == -73.9999  # to_lon positional arg


async def test_replan_falls_back_to_stop_index_when_to_lat_absent(conn):
    trip = create_monitored_trip(
        conn, "anon-1",
        Itinerary(duration_seconds=900, legs=[
            _subway_leg("F", "A1", "A2", "Roosevelt Island", "Transfer Stop", _local_ms(2026, 9, 1, 8), _local_ms(2026, 9, 1, 8, 20)),
        ]),
        deadline_ts=None,
    )
    # This trip's stored leg has no to_lat/to_lon (an old trip predating
    # Task 1) -- must still resolve via stop_index, not crash.
    with patch("app.agents.replan_agent.OTPClient.plan_route", AsyncMock(return_value=[])):
        result = await replan_trip(trip, "test", conn=conn)

    assert result is None  # OTP found nothing, but it didn't crash resolving the destination
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_replan_agent.py -k "true_destination or falls_back" -v`
Expected: FAIL (or the first assertion mismatches) — `replan_trip` currently always resolves via `stop_index.find_by_id`, ignoring `to_lat`/`to_lon` entirely.

- [ ] **Step 7: Update `replan_trip`'s destination resolution and notification text**

In `backend/app/agents/replan_agent.py`, change this section of `replan_trip`:

```python
        stop_index = get_stop_index()
        origin_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[0].from_stop_id))
        dest_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[-1].to_stop_id))
        if origin_stop is None or dest_stop is None:
            return None  # a stop that no longer resolves against static GTFS

        otp = OTPClient(base_url=settings.otp_base_url)
        itineraries = await otp.plan_route(
            origin_stop["lat"], origin_stop["lon"], dest_stop["lat"], dest_stop["lon"]
        )
```

to:

```python
        stop_index = get_stop_index()
        origin_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[0].from_stop_id))
        if origin_stop is None:
            return None  # a stop that no longer resolves against static GTFS

        # The true, originally-stated destination doesn't change over the
        # trip's lifetime (unlike the origin, which goes stale the moment
        # the user starts moving) -- prefer it directly over the
        # last-transit-stop approximation whenever Task 1's lat/lon fields
        # are present on the stored snapshot's final leg.
        last_leg = trip.itinerary_snapshot.legs[-1]
        if last_leg.to_lat is not None and last_leg.to_lon is not None:
            dest_lat, dest_lon = last_leg.to_lat, last_leg.to_lon
        else:
            dest_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[-1].to_stop_id))
            if dest_stop is None:
                return None
            dest_lat, dest_lon = dest_stop["lat"], dest_stop["lon"]

        otp = OTPClient(base_url=settings.otp_base_url)
        itineraries = await otp.plan_route(
            origin_stop["lat"], origin_stop["lon"], dest_lat, dest_lon
        )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_replan_agent.py -v`
Expected: PASS, all tests.

- [ ] **Step 9: Incorporate condensed walk legs into `_build_notification`**

Change `_build_notification`'s signature and body to accept the full new itinerary's legs (not just `transit_legs`), condensing any WALK leg sitting between/after transit legs:

```python
def _build_notification(new_itinerary: Itinerary, new_risks: list, depart_by_ts: int | None) -> str:
    route_summary = _route_summary(new_itinerary)
    citation_risk = next((r for r in new_risks if r.quality == "ok"), None)

    walk_notes = []
    for leg in new_itinerary.legs:
        if leg.mode == "WALK" and leg.steps:
            condensed = condense_walk_steps(leg.steps)
            if condensed:
                walk_notes.append(condensed)
    walk_suffix = f" {' '.join(walk_notes)}" if walk_notes else ""

    if citation_risk is None:
        text = f"Your trip has been rerouted: now via {route_summary}.{walk_suffix} No transfer risk to report for the new route."
        if depart_by_ts is not None:
            text += f" Based on the new route, you should now aim to leave by {_format_departure_clock(depart_by_ts)}."
    else:
        pct = round(citation_risk.p_miss * 100)
        footer = CITATION_FOOTER_TEMPLATE.format(n=round(citation_risk.n), window_days=citation_risk.window_days)
        body = (
            f"Your trip has been rerouted: now via {route_summary}.{walk_suffix} There's about a "
            f"{pct}%* chance of missing the {citation_risk.from_route}->{citation_risk.to_route} "
            f"transfer at {citation_risk.transfer_stop_name}."
        )
        if depart_by_ts is not None:
            body += f" Based on the new route, you should now aim to leave by {_format_departure_clock(depart_by_ts)}."
        text = f"{body}\n{footer}"

    return text
```

- [ ] **Step 10: Write a test proving walk legs reach the notification text**

Add to `backend/tests/test_replan_agent.py`:

```python
def test_build_notification_includes_condensed_walk_directions():
    from app.agents.replan_agent import _build_notification

    itinerary = Itinerary(duration_seconds=900, legs=[
        _subway_leg("Q", "A2", "A3", "Transfer Stop", "Lex/63", _local_ms(2026, 9, 1, 8), _local_ms(2026, 9, 1, 8, 10)),
        Leg(
            mode="WALK", from_stop_name="Lex/63", to_stop_name="2 West Loop Rd",
            start_time_ms=_local_ms(2026, 9, 1, 8, 10), end_time_ms=_local_ms(2026, 9, 1, 8, 15),
            steps=[WalkStep(street_name="Main St", distance_meters=50, relative_direction="LEFT")],
        ),
    ])

    text = _build_notification(itinerary, [], None)

    assert "turn left on Main St" in text.lower()
```

- [ ] **Step 11: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_replan_agent.py -v`
Expected: PASS, all tests.

- [ ] **Step 12: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 13: Commit**

```bash
git add backend/app/agents/replan_agent.py backend/tests/test_replan_agent.py
git commit -m "Add walking directions and true-destination resolution to re-plan notifications"
```

---

## Execution

Tasks 1-7 have real sequential dependencies (Task 1's model fields are consumed by Tasks 2, 3, 6, 7; Task 3's `user_location` is consumed by Task 6) but each is independently reviewable — a reviewer could approve Task 2's prompt wording while rejecting Task 5's Geoclient error handling, for instance. Suitable for subagent-driven-development, dispatched in order (1 → 2 → 3 → 4 → 5 → 6 → 7), since later tasks' briefs assume earlier tasks' interfaces already exist.
