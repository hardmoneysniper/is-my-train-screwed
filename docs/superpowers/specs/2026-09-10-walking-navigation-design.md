# Walking Navigation & Location Input — Design

Spec: `is-my-train-screwed-spec.md` (no existing section covers this — confirmed via search, this is new scope, not a gap in an already-specified feature).

## Position in the project

Not one of the original 5 phases. An enhancement to the already-shipped core planner (Phase 1) and risk/monitoring stack (Phases 2-3): it extends what `plan_route` returns and how both the Conversation Agent (live chat) and the Re-plan Agent (background monitoring) narrate it, and adds two new ways to tell the system where a trip starts.

## Motivation

The current product only narrates transit legs (route names, stop names, times). It never tells a user how to actually walk to the first stop, how to walk between two stations during a transfer, or how to walk from the last stop to where they're actually going — despite this data being real and available from OTP. Separately, the only way to specify an origin/destination today is a named transit stop (via `find_stop`) — there's no way to say "from here" or type a street address, which means the walking-navigation gap rarely even triggers in practice (a stop-to-stop trip has no meaningful walk legs).

## Real data verified live this session (not assumed)

Introspected OTP's actual GraphQL schema and ran a real `plan` query against the live deployed `otp` Railway service (via `railway ssh` into `backend`, reaching `otp.railway.internal:8080` over private networking — no Docker needed). Confirmed:

- `Leg.steps: [step]` — real turn-by-turn walk data: `streetName` (String, sometimes a generic OSM value like `"sidewalk"`/`"path"`, not always a real street name — that's genuine data, not a bug), `distance` (Float, meters), `relativeDirection` (ENUM), `absoluteDirection` (ENUM), `exit`, `stayOn`. Populated on WALK legs, empty (`[]`) on transit legs.
- `RelativeDirection` enum's full real value set: `CIRCLE_CLOCKWISE`, `CIRCLE_COUNTERCLOCKWISE`, `CONTINUE`, `DEPART`, `ELEVATOR`, `ENTER_STATION`, `EXIT_STATION`, `FOLLOW_SIGNS`, `HARD_LEFT`, `HARD_RIGHT`, `LEFT`, `RIGHT`, `SLIGHTLY_LEFT`, `SLIGHTLY_RIGHT`, `UTURN_LEFT`, `UTURN_RIGHT`.
- `Leg.headsign: String` — real headsign text matching actual train rollsign/platform display (e.g. "Middle Village-Metropolitan Av" for a real M train, "96 St" for a real Q train at Lex/63). This is the one reliable, verifiable "which train to board" disambiguation signal available.
- `Stop.platformCode`, `Stop.direction`, `Stop.code` — all confirmed **`null`** for every real subway stop tested (Roosevelt Island, Lex/63, 72 St). MTA's static subway GTFS feed does not populate these. Platform-number-level guidance is not buildable from this data source — ruled out, not attempted.
- `Place.lat`/`Place.lon` (on `Leg.from`/`Leg.to`) — confirmed **non-null, real** on every leg endpoint, including WALK-leg endpoints that aren't GTFS stops. This directly resolves a limitation documented in Phase 3 Task 6 (which avoided extending `Leg` with lat/lon because it had no way to verify OTP's schema live at the time — that blocker no longer applies, verified now).

## Architecture

### 1. Data model (`backend/app/models/transit.py`)

```python
class WalkStep(BaseModel):
    street_name: str | None = None
    distance_meters: float
    relative_direction: str | None = None
    absolute_direction: str | None = None
    exit: str | None = None
    stay_on: bool = False

class Leg(BaseModel):
    # ...all existing fields unchanged...
    headsign: str | None = None
    steps: list[WalkStep] = []
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None
```

All eight new fields are optional with safe defaults — deliberately, even though OTP always returns real values for `steps`/`headsign`/lat/lon on a fresh `plan_route` call. Reasoning: making them required would break every existing hand-constructed `Leg(...)` across `test_risk_engine.py`, `test_deadline.py`, `test_conversation_agent.py`, `test_monitoring.py`, `test_replan_agent.py`, `test_trip_monitor.py` (dozens of call sites across four already-shipped, already-reviewed tasks) — a large, unjustified ripple for a type-strictness gain with no functional benefit. Optional-with-defaults means: existing tests need zero changes, and any *old* stored `monitored_trips.itinerary_snapshot` row (pre-dating this change) still deserializes correctly via Pydantic defaults — no migration needed, matching this project's established "no migration framework, `CREATE TABLE IF NOT EXISTS`" convention.

`backend/app/routing/otp_client.py`'s `PLAN_QUERY` extended to request `headsign`, `steps { streetName distance relativeDirection absoluteDirection exit stayOn }`, and `lat`/`lon` on both `from` and `to` for every leg — all confirmed-live field names above.

### 2. Location input — three ways to specify an origin/destination

| Input | Mechanism | Status |
|---|---|---|
| Named transit stop | `find_stop` tool (existing) | Already shipped |
| "From here" / current location | Browser geolocation, see below | New, this design |
| Typed street address | NYC Geoclient API, see below | New, this design — blocked on a credential the user is obtaining |

**Geolocation ("current location")**: no proactive permission prompt on page load. A cheap client-side keyword check (case-insensitive substring match against a fixed phrase list: "here," "my location," "near me," "current location," "where i am") on the outgoing message text triggers `navigator.geolocation.getCurrentPosition()` at send time, the *first* time it matches in a session — a real user gesture, satisfying browser permission requirements. Once granted, the coordinates are cached in memory and attached to **every** subsequent `/chat` request for the rest of the session as a new optional `ChatRequest.user_location: {lat, lon} | None` field (re-requesting only if the cached value is older than 5 minutes) — not re-triggered by the keyword check on every message. This matches `anonymous_id`'s existing "always sent, the agent decides per-message whether it's relevant" pattern rather than a per-message opt-in, and avoids re-prompting the keyword check redundantly once location is already known for the session.

`user_location`, when present, is injected into the **per-call user message only** — `[User's current location: lat, lon]` — never into the cached `SYSTEM_PROMPT` block. This is the exact same pattern Phase 3 Task 5 already established for injecting the current timestamp (needed for LLM-side deadline parsing) without poisoning Anthropic's prompt cache. Reusing a proven mechanism, not inventing a new one.

`SYSTEM_PROMPT` addition: when the user references their current location and `user_location` is present, use those coordinates directly for `plan_route` — never call `find_stop` for this case. If `user_location` is absent, ask the user to share their location or name a place instead — never guess coordinates.

**Address geocoding (NYC Geoclient API)**: new `geocode_address(address: str) -> {lat, lon} | None` backend function and matching `find_address` tool (parallel to `find_stop`), calling NYC's Geoclient API. Scoped to NYC only, per your direction — this product's actual routing coverage (OTP's graph, GTFS feeds) is NYC-only anyway, so no plausibility/bounding-box check beyond what a NYC-scoped geocoder already guarantees. **Blocked on a credential** (`GEOCLIENT_API_KEY` or similar — exact env var name TBD when the real key format is known): you're obtaining this yourself from `api-portal.nyc.gov` (or wherever NYC's current developer portal actually is — needs live confirmation at implementation time, not assumed from training data, same discipline as this project's MTA endpoints). This task's brief will build the real function and its tests against a documented/mocked shape, but live verification (the real endpoint URL, real auth header, real response shape) waits for the key — matching how this project's email-API-key gap was handled (build what doesn't depend on it, flag what's blocked, don't fake it).

### 3. Primary chat narration (LLM-driven, no new Python formatting)

The Conversation Agent already narrates raw `plan_route` JSON into prose for every other leg detail (route names, stop names, times) — no pre-processing layer exists for that today, and none is added here. `steps`/`headsign` ride along in the same raw JSON `_handle` dispatch already sends as a tool result; `SYSTEM_PROMPT` gets new instructions:
- Condense walk legs into 1-2 natural sentences using the real street names/distances/turns, collapsing trivial micro-segments — don't mechanically recite every OTP step.
- Never invent a street name OTP didn't return (a genuinely-generic value like `"sidewalk"` gets narrated as generic, e.g. "continue along the sidewalk," not dressed up).
- At every boarding and every transfer, state "board the {route} toward {headsign}" using the real headsign text — this applies universally (per your earlier choice), not only where `get_risk` has a citable number, since it's navigation guidance, not a risk statement.

### 4. Re-plan notification enrichment (`replan_agent.py` — template-only, no LLM, real new Python work)

Phase 3's Re-plan Agent is template-first by design (no LLM for the common case, per spec §9.1's cost envelope) — it can't lean on LLM narration the way the primary chat path does. Two real, buildable enrichments, one deliberate boundary:

**Buildable — mid-trip walking transfers.** Any WALK leg sitting between two transit legs in the freshly re-planned itinerary is real and correctly modeled today (Task 6's `replan_trip` already re-plans between two real GTFS-resolved stop coordinates, so a walking transfer within that span is exactly as real as in a fresh `plan_route` call). New deterministic step-condenser in `replan_agent.py`: merge consecutive steps sharing the same `relative_direction`, map each of OTP's real direction enum values to a fixed phrase (complete table for all 16 confirmed-live enum values — `LEFT`→"turn left", `SLIGHTLY_RIGHT`→"bear right", `DEPART`→"head {absolute_direction}", `ENTER_STATION`→"enter the station", etc.), join into one or two sentences. This is legitimate non-fabricating work — a lookup table over real OTP-provided values, the same pattern already used for citation-format rendering elsewhere in this codebase, not the LLM inventing anything.

**Buildable — the destination-side final walk.** Reconsidered from Task 6's original scope: the *origin* goes stale once the user starts moving (re-planning "from where they started" would send them backward, since there's no live position at re-plan time — this remains correctly out of scope, same reasoning as the design doc's existing deferred-geofencing decision). But the **stated destination doesn't change over the trip's lifetime** — it's fixed at creation and stays fixed until the trip ends. `replan_trip`'s destination resolution changes from "last transit stop via `StopIndex`" to `trip.itinerary_snapshot.legs[-1].to_lat`/`to_lon` directly (the *true* originally-stated destination, now available thanks to the `Leg` model extension above) — falling back to the current `StopIndex`-based station resolution only if those fields are absent (an old stored trip predating this change). Re-planning to the true destination means a genuine trailing WALK leg (station → true destination) appears in the fresh itinerary whenever the destination isn't exactly at a stop, formatted through the same step-condenser as the mid-transfer case.

**Origin-side walk in re-plan notifications — deliberately still out of scope**, same reasoning as Task 6's original documented simplification: there is no live user position available to a background poll cycle, and the trip's original starting coordinates go stale the moment the user starts moving. Re-planning "from the true origin" would be actively wrong (routes the user backward), not just incomplete. This is restated explicitly here, not silently carried over — it's the one piece of your two questions that stays a real, acknowledged architectural boundary.

## Error handling

- Geolocation denied/unavailable/stale (>5 min) → no `user_location` sent; agent asks for a place, address, or permission instead of guessing.
- Geocoding failure (API down, no match, credential missing/invalid) → `find_address` returns an honest "couldn't find that address" signal; agent asks the user to clarify or try a stop name instead — never guesses coordinates. Matches this product's `n<200` → `"insufficient"` honesty pattern in spirit: absence of real data never gets papered over.
- A step with a null `street_name` → narrated/formatted as a generic continuation, never given an invented name.
- An old `monitored_trips` row with no `to_lat`/`to_lon` on its final leg → `replan_trip` falls back to the pre-existing station-based destination resolution, doesn't crash.

## Testing

- `otp_client` parsing tests extended with a fixture matching the real live-verified GraphQL response shape captured this session (real field names, real enum values, real `null` platform fields).
- Regression test: an old-shaped stored `itinerary_snapshot` JSON (no `steps`/`headsign`/lat-lon keys) still round-trips through `Itinerary.model_validate_json` correctly.
- `replan_agent`'s step-condenser gets direct unit tests (deterministic, hand-computable expected output — matches this project's TDD convention) covering: consecutive-same-direction merging, every one of the 16 real enum values' phrase mapping, a null-`street_name` step.
- `replan_trip` destination-resolution test: a trip whose `itinerary_snapshot` has real `to_lat`/`to_lon` produces a re-plan itinerary using those exact coordinates (assert via a mocked `plan_route` call's arguments); a trip without them falls back to the existing station-resolution path unchanged.
- Conversation-agent tests confirm the tool-result JSON reaching the LLM carries real `headsign`/`steps` data — same "can't test LLM prose quality, can test the data is real and present" honesty caveat as every existing prompt-driven test in this codebase.
- Frontend tests for the location-phrase heuristic and geolocation attachment/staleness, mocking `navigator.geolocation`, matching `client.test.ts`'s existing `anonymous_id` test conventions.
- `find_address`/Geoclient: built and tested against a documented/mocked response shape now; a real live-verification pass (confirming actual endpoint, auth header, response shape) happens once the credential is available — flagged explicitly as a blocked step, not silently skipped.

## Explicitly out of scope

- Origin-side walk in re-plan notifications (see above — a real architectural boundary, not an oversight).
- Platform-number/entrance-level guidance (confirmed unavailable in MTA's static GTFS data via OTP).
- Any map/visual UI for walking directions — chat-narrated text only, matching this product's existing "no separate UI surface" principle (spec §8/§9).
- Geocoding outside NYC, or a plausibility/bounding-box check beyond what a NYC-scoped geocoder already guarantees (per your direction — this product's actual coverage is NYC-only regardless).
