# Is My Train Screwed? — Project Specification v1.0

**Author:** Frank (Rongyu Shen) · Cornell Tech M.S. Information Systems (Urban Tech)
**Purpose of this document:** Implementation spec for Claude Code. Build in the phase order given. Ask before deviating from architecture decisions marked **[DECISION]**.

---

## 0. Kickoff Protocol — do this FIRST, before writing any code

### 0.1 Requirements manifest (output to Frank, then wait)

Before implementing anything, produce a single consolidated checklist titled **"What I need from you to proceed"** listing every external dependency this project requires, split into:

1. **Credentials/keys Frank must obtain:** MTA BusTime API key (register at the MTA developer portal), Anthropic API key (with instructions to set a hard spend cap), transactional email API key (Resend or Postmark free tier), hosting account if deploying (Fly.io/Railway). For each: where to sign up, expected cost (should be $0 except Anthropic), and the env var name it will live in.
2. **Data assets to download:** MTA subway + bus static GTFS zips, subwaydata.nyc backfill range (recommend 60–90 days; provide the exact download loop), and confirmation of which real-time feed URLs are current (verify against the MTA developer portal at kickoff — do not trust training data for endpoint URLs).
3. **Access needed:** local path or GitHub URL of the mini-nyc-3d repo (see §0.2), GitHub repo + token if the Feedback Triage Agent will file issues.

Rules: never mock, stub, or hardcode fake credentials to "keep moving" — if something on the manifest is missing, build the components that don't depend on it and clearly flag what is blocked. Re-verify all MTA endpoint URLs and registration steps against current MTA developer documentation at kickoff, since these change.

### 0.2 mini-nyc-3d reuse audit — maximize reuse, don't rebuild

Frank's existing project **mini-nyc-3d** (fork of mini-tokyo-3d, adapted to live MTA data) already solves subway data access. Before writing any new MTA integration code:

1. **Audit it:** read its data-access layer and document, in a short `REUSE.md`, exactly which of its components map to this project's needs — GTFS-RT feed endpoint URLs and auth handling, protobuf/NYCT-extension parsing, feed-polling cadence and retry logic, stop/route ID conventions and any static GTFS handling.
2. **Reuse decision per component:** import/port directly (preferred where license permits — check upstream mini-tokyo-3d license and preserve attribution), adapt with modification, or justify why new code is genuinely needed. "I didn't look" is not an acceptable justification; the default is reuse.
3. Where mini-nyc-3d's code is JS/TS and this backend is Python, port the *logic and endpoint knowledge* (URLs, field mappings, quirks like NYCT trip-ID formats) rather than the code itself, and note the provenance in comments.
4. Flag to Frank any place where mini-nyc-3d's approach conflicts with this spec (e.g., polling cadence, in-memory vs. persisted state) instead of silently choosing.

---

## 1. Product Overview

A conversational, agentic NYC transit trip advisor delivered as a **PWA** (single codebase, installable on mobile, web-push capable). Unlike Google Maps / Citymapper / Transit, this product does not compete on routing — routing is commodity. It competes on **judgment**:

1. **Proactive trip monitoring** — the agent watches a planned trip against live MTA data and pushes re-plans before the user is stranded ("F just went delayed — leave by 11:40 or switch to tram + Q70").
2. **Empirical risk quantification** — transfer-miss probabilities computed from observed historical arrival data, never hallucinated by an LLM ("42 min, but 30% chance you miss the Q70 connection — safe plan is 55 min"). Every probability displays provenance (n observations, window).
3. **Accessibility & luggage-aware routing** — consumes the MTA real-time elevator/escalator outage feed; "traveling with a suitcase" mode avoids broken elevators and stair-only transfers.
4. **Airport-run domain depth** *(data-gated, see §2)* — deadline-anchored planning ("my flight is at 2pm") that plans backward with conservative percentiles, encodes terminal-level bus knowledge (Q70 free w/ luggage racks, terminal stops), and security-buffer heuristics.

**Beachhead:** general NYC subway trips starting from Roosevelt Island / Cornell Tech riders (their daily commutes, not just special trips). First users: Cornell Tech students. **Airport mode (LGA, then JFK) activates as an expansion once bus-corridor data matures** — collection starts day one, but the feature ships only when its probabilities are real.

**Narrative goal:** a multi-agent system that *operates itself* — narrow-scope agents with explicit decision boundaries, safe autonomous action spaces, and human escalation. Not a "swarm"; a disciplined pipeline.

---

## 2. Scope

### In scope (v1)
- **Launch surface: NYC Subway** — full network routing + citywide reliability data (bootstrap from subwaydata.nyc archives + own GTFS-RT collection). Subway trips get probabilities and monitoring from launch.
- Bus: **transfer corridors only**, config-driven, **collection starts day one but corridor features are data-gated** — a corridor's probabilities and airport mode surface only when its buckets cross the `n≥200` threshold. Seed collection set: Q70 (Jackson Hts–Roosevelt Av ↔ LGA), M60 (125 St ↔ LGA), Roosevelt Island Tram (static schedule + manual status).
- **Airport mode:** LGA first (Q70/M60 corridors); JFK considered next **if data permits** — via subway-to-Jamaica or Howard Beach legs (fully covered by subway data) plus AirTrain as a static-schedule leg; any JFK bus corridor (e.g. Q3, B15) added only through the coverage-expansion pipeline on demand.
- Trip planning, monitored trips with notifications, risk-quantified answers, accessibility mode, deadline mode (deadline mode works for any trip type from launch — meetings, interviews — not only flights).
- In-chat feedback pipeline with agent triage and the coverage-expansion loop.

### Out of scope (v1) — deliberate decisions, note in README roadmap
- LIRR / Metro-North (incl. JFK via Jamaica AirTrain — subway to Jamaica suffices for v1; flagged as top roadmap item).
- Native iOS/Android apps (PWA only).
- Fare calculation, OMNY integration.
- Multi-city.

---

## 3. Architecture

### 3.1 High-level components

```
┌─────────────────────────────────────────────────────┐
│ PWA Frontend (chat UI + trip cards + map snippet)   │
└──────────────┬──────────────────────────────────────┘
               │ REST/WebSocket
┌──────────────┴──────────────────────────────────────┐
│ API Server (FastAPI)                                │
│  ├─ Conversation Agent (LLM, tool-calling)          │
│  ├─ Routing Service (OpenTripPlanner wrapper)       │
│  ├─ Risk Engine (pure code, no LLM)                 │
│  ├─ Trip Monitor Service (pure code, no LLM)        │
│  ├─ Re-plan Agent (LLM, triggered only)             │
│  ├─ Feedback Triage Agent (LLM)                     │
│  └─ Coverage-Expansion Agent (LLM + validators)     │
├─────────────────────────────────────────────────────┤
│ Data Layer                                          │
│  ├─ SQLite/Postgres: reliability aggregates,        │
│  │   trips, users, feedback, coverage config        │
│  ├─ GTFS static (subway + bus) → OTP graph          │
│  └─ Collectors: subway GTFS-RT, BusTime SIRI        │
│     (corridor-filtered), elevator/escalator feed    │
└─────────────────────────────────────────────────────┘
```

**[DECISION] LLM agents never compute numbers or routes.** They orchestrate tools and narrate results. Routing comes from OTP; probabilities come from the Risk Engine; monitoring is a dumb poller. LLM calls happen only at: conversation turns, triggered re-plans, feedback triage, coverage validation.

### 3.2 Agents and their decision boundaries

| Agent | May do autonomously | Must escalate |
|---|---|---|
| Conversation | Answer trip queries via tools; ask clarifying Qs (deadline? luggage?) | — |
| Re-plan | Compose notification text from monitor triggers + new route from OTP | Never invents routes/probabilities |
| Feedback Triage | Classify, dedupe, score priority, file GitHub Issues, weekly email digest | Implement/reject is a *recommendation* only |
| Coverage-Expansion | Validate requested route against static GTFS + corridor plausibility → append to collector config → confirm to user | Anything requiring code change → email Frank |
| Trip Monitor (not an LLM) | Poll, diff, TTL cleanup, fire triggers | — |

**[DECISION] Safe autonomous action space:** the only self-modifying action any agent can take is appending a validated entry to the bus-corridor collector config. Worst-case failure: harmless extra polling.

---

## 4. Data Sources

| Data | Source | Notes |
|---|---|---|
| Subway static GTFS | MTA developer portal | Feeds OTP graph + stops.txt for nearest-stop |
| Subway GTFS-RT | MTA (NYCT protobuf extensions; `nyct-gtfs` lib acceptable) | Live predictions + alerts; also logged by collector |
| Bus static GTFS | MTA developer portal | Full network in OTP (routing works everywhere) |
| Bus real-time | MTA BusTime API (SIRI / GTFS-RT), API key required | **Collected only for corridors in coverage config** |
| Historical subway trips | subwaydata.nyc per-day CSVs: `https://subwaydata.nyc/data/subwaydatanyc_YYYY-MM-DD_csv.tar.xz` | Bootstrap corpus for reliability aggregates |
| Elevator/escalator outages | MTA real-time E&E feed | Accessibility mode |
| Service alerts | MTA alerts feed | Monitor triggers |

Historical bus data: none available retroactively → self-collect from launch; corridor stats usable after ~2–3 weeks. UI must show "collecting — probabilities available soon" for young corridors.

---

## 5. Risk Engine (empirical probabilities)

**Core insight: transfers decompose.** Never store per-transfer-pair data. Store per-**(route, stop_id, direction, day_type, hour_bucket)** distributions; compose any transfer at query time.

- **Storage:** sufficient statistics only — headway/delay histograms or t-digest sketches + observation count per bucket. ~150K rows; fits SQLite. Raw CSVs archived cold after aggregation.
- **Update:** nightly batch ingests day's collected feed (and backfills subwaydata.nyc); rolling update via exponential decay (`new = 0.95·old + 0.05·today`).
- **Query-time:** fetch incoming-leg arrival distribution + outgoing-leg headway distribution at transfer stop → Monte Carlo (~1,000 samples) or convolution → `p_miss`. Milliseconds, no LLM.
- **Live+historical blend:** live GTFS-RT prediction sets the distribution center; historical bucket supplies spread (observed prediction-error variance at that stop/hour).
- **Provenance:** every displayed probability carries `n` and window ("based on 3,400 observed F arrivals here, last 30 days"). If `n < threshold` (e.g. 200), show route without probability + "still collecting."

**[DECISION] The LLM receives `{p_miss: 0.31, n: 3400, window_days: 30}` as tool output and may only narrate it. It never estimates probabilities.**

### 5.1 What "probability" means in this product — READ CAREFULLY

`p_miss` and every other displayed probability is an **empirical frequency computed by deterministic code from logged observations**. It is NOT:
- an LLM estimate, guess, or "reasoning" output;
- a hardcoded heuristic ("assume 20% during rush hour");
- a number pulled from schedules alone.

The computation is: (1) collect timestamped arrival events from real-time feeds; (2) aggregate them into per-bucket distributions; (3) at query time, sample those distributions to answer "in what fraction of observed-history-like outcomes does this transfer fail?" If Claude Code ever finds itself writing a prompt that asks a model to produce a probability, or inventing a constant, that is a spec violation — the correct fix is always more/better observation data or an honest "insufficient data (n<200)" response. Degrading honestly is a feature; a made-up number is a bug.

### 5.2 Bus Observation Collection — Schema & Derivation

**[DECISION] One agency-agnostic schema for subway and bus.** Subway rows come from GTFS-RT + subwaydata.nyc backfill; bus rows come from the BusTime collector below. Same tables, `agency` discriminator.

**Collector behavior (bus):** poll BusTime (SIRI `StopMonitoring` or GTFS-RT equivalent) every 30s, **only for (route, stop) pairs present in the coverage config** (`coverage_corridors` table: corridor_id, agency, route_id, stop_ids[], added_by [seed|expansion_agent], added_at, status [collecting|live]).

**Raw polling snapshots** (append-only, ndjson on disk, rotated daily — cheap and replayable):
```
{ polled_at, agency, route_id, direction, stop_id, vehicle_id,
  trip_id?, predicted_arrival_ts?, distance_along_route?, raw_source }
```

**Derived arrival events** — the unit the Risk Engine consumes. An arrival event is emitted by the collector's state machine when a tracked vehicle transitions from *approaching* a stop (present in the stop's monitored calls / decreasing predicted arrival) to *past* it (absent from monitored calls, or distance beyond stop). Table `arrival_events`:
```
agency TEXT, route_id TEXT, direction TEXT, stop_id TEXT,
vehicle_id TEXT, trip_id TEXT NULL,
observed_arrival_ts TIMESTAMP,        -- interpolated moment of passage
scheduled_arrival_ts TIMESTAMP NULL,  -- if matched to static GTFS trip
delay_seconds INT NULL,               -- observed - scheduled, when matched
predicted_arrival_ts_at_T_minus_5 TIMESTAMP NULL,  -- feed's prediction 5 min prior (for prediction-error stats)
service_date DATE, day_type TEXT,     -- weekday|weekend (holidays → weekend)
hour_bucket INT,                      -- 0–23, local time
derivation_quality TEXT               -- clean|interpolated|ambiguous
```
Rules for Claude Code:
- Deduplicate: one event per (vehicle_id, stop_id, service_date, ~window); vehicles can re-appear in feeds — guard with a per-vehicle last-seen state.
- If passage moment is ambiguous (polling gap > 90s around passage), still emit with `derivation_quality='ambiguous'`; the aggregator weights these at 0.5 or excludes them for prediction-error stats.
- Never fabricate events from schedule data. Schedule is used only for matching/delay computation, never as a substitute observation.
- **Headways are derived downstream**, not stored per-event: nightly aggregator sorts events per (agency, route, stop, direction, service_date) by time and diffs consecutive `observed_arrival_ts`.

**Aggregate table** (what query-time actually reads) — `reliability_buckets`:
```
agency, route_id, stop_id, direction, day_type, hour_bucket,
stat_type TEXT,          -- headway | delay | prediction_error
histogram JSON,          -- fixed bins (e.g. 30s-wide, 0–40min) OR t-digest blob
n_observations INT, n_ambiguous INT,
window_start DATE, last_updated TIMESTAMP
```
Nightly job: fold yesterday's events into each bucket with exponential decay (`hist = 0.95·hist + 0.05·yesterday`), update `n` with the same decay so provenance reflects the effective window. Raw ndjson older than 90 days → cold archive.

**Query-time contract** (Risk Engine, pure function):
`get_risk(itinerary) → for each transfer: sample incoming-leg arrival dist (live prediction as center, bucket's prediction_error as spread) × outgoing-leg headway dist → Monte Carlo (~1000 draws) → { p_miss, n, window_days, quality }`. If `n < 200` for any required bucket → return `quality: "insufficient"` and the API/LLM must present the route without a probability.

---

## 6. Trip Monitoring Lifecycle

- Monitored trip = server-side job (no LLM). Polls alerts + trip updates every 60s, diffs against planned itinerary segments.
- **Trigger fires** (alert on a segment, headway anomaly, elevator outage on an accessibility-mode trip) → wake Re-plan Agent → OTP re-query + Risk Engine → push notification with revised plan.
- **Lifecycle / termination (in priority order):**
  1. Geofence arrival (if location permission granted) → complete.
  2. Explicit action on any notification: "I'm here / Cancel trip."
  3. TTL: auto-expire at scheduled arrival + 30 min. Silent cleanup.
- Cost envelope: typical monitored trip = 0–3 LLM calls total.

**Deadline mode:** if destination/type implies stakes (airport, address matching flight/interview patterns) or user confirms "short on time," plan backward from the deadline using a conservative percentile (e.g. p85 travel time) and monitor against the deadline, not just the route.

---

## 7. Coverage Boundaries & Expansion Loop

Two distinct failure modes — different UX:

**A. Plannable but unmonitored** (route exists in static GTFS, no reliability data):
> Serve the route. Flag: "I don't have reliability data for the B41 yet — no delay probabilities or live monitoring on that leg. Want me to add it to coverage?" `[Yes, add it]` `[No thanks]`

**B. Out of scope** (LIRR/MNR leg, outside service area):
> Single immediate message: "Sorry — I don't cover Metro-North yet, so I can't plan that leg reliably. Want me to log this so it gets prioritized? I can email you when it's live (optional): [email field] `[Log request]`"

**Never fabricate a plan in case B.** Agent auto-generates the structured expansion request (origin, destination, missing route/agency, timestamp) — user only consents, never fills a form.

**Expansion pipeline:** request → Coverage-Expansion Agent validates (route exists in GTFS? plausible transfer corridor / in-scope agency?) →
- Bus corridor: append to collector config, reply "Added — probability data in ~2 weeks," notify when `n` crosses threshold.
- Requires code/architecture (e.g. new agency): file + email Frank with the demand count.

**Notification of fulfillment (cascade, cheapest first):**
1. **On-return notice** — anonymous ID (localStorage or light account) stores pending requests; next visit, agent greets: "Since your last visit: B41 corridor is live." Build first.
2. **Optional transactional email** at consent tap (Resend/Postmark).
3. **Web Push** (PWA, incl. iOS/Android when installed) — request permission only after a user has an active monitored trip, never on first visit.

---

## 8. Feedback Pipeline (in-chat)

**[DECISION] No separate feedback window.** The Conversation Agent detects feedback intent in the normal chat stream. One persistent "Report an issue" link (corner) as escape hatch for broken-chat / screenshot cases.

Three lanes by required action:
1. **Auto-actionable:** coverage requests (see §7). Closed loop, fully autonomous within the validated config action space.
2. **Triaged:** bugs / feature requests / UX — classify, dedupe against open GitHub Issues, priority = frequency × severity, file issue, weekly ranked email digest to Frank with implement/reject recommendation + reasoning. Frank decides.
3. **Discarded:** spam / zero-signal — counted (the count is a metric), never surfaced.

---

## 9. Tech Stack

- **Frontend:** React + Vite PWA (service worker, installable, Push API). Chat-first UI; trip cards with route legs, probability badges w/ provenance tooltip, monitor status. Follow the frontend-design skill for visual identity — this product should not look like a default AI app.
- **Backend:** Python FastAPI. Pydantic models throughout (reuse Frank's CDS modeling patterns; agency-agnostic transit models to keep LIRR/MNR door open).
- **Routing:** OpenTripPlanner 2 (Docker) loaded with MTA subway + bus static GTFS. Backend wraps its GraphQL API. Nearest-stop = stops.txt in a spatial index (Shapely/rtree) — do not hand-roll routing.
- **Collectors:** async Python workers — subway GTFS-RT (all lines), BusTime (corridor config only), E&E outages, alerts. Write raw to disk, aggregates to DB nightly. Reuse ingestion patterns from Frank's mini-nyc-3d project (already consumes MTA GTFS-RT) where applicable.
- **DB:** SQLite to start (aggregates ~150K rows); Postgres migration path noted.
- **Email:** Resend or Postmark (transactional only).
- **Deploy:** single VPS or Fly.io/Railway; collectors as background workers; OTP as sidecar container.
- **LLM:** Anthropic API, tool-calling. Tools: `plan_route`, `get_risk`, `create_monitored_trip`, `log_feedback`, `request_coverage`, `get_alerts`, `get_elevator_status`. See §9.1 for model routing.

### 9.1 Model Routing & Cost Controls

**[DECISION] Model is a config parameter per agent (env/config file), never hardcoded.** Default routing:

| Agent / task | Model | Rationale |
|---|---|---|
| Conversation Agent | Haiku 4.5 (`claude-haiku-4-5-20251001`) to start; escalate to Sonnet only if eval shows tool-call errors | Tool orchestration + narration; intelligence lives in tools |
| Re-plan notifications | **String templates first** ("{line} delayed at {station} — leave by {time} or take {alt}"); Haiku only for multi-tradeoff re-plans | ~90% of notifications are templatable; zero-LLM path preferred |
| Feedback Triage | Keyword/regex pre-filter for obvious spam → Haiku for classification, dedupe, priority scoring | Classification is Haiku's sweet spot |
| Coverage-Expansion validation | Mostly pure code (GTFS lookup); Haiku only for parsing free-text requests into structured form | Validation logic must be deterministic |
| Weekly digest, backfill jobs | Haiku via **Batch API** (50% discount; stacks with caching) | Nothing here is latency-sensitive |
| Any task | Never Opus/flagship tier | Nothing in this product requires it |

**Cost controls (implement from day one):**
- **Prompt caching** on the system prompt + tool definitions for the Conversation Agent (identical every turn; cache hits ~10% of input price).
- **Hard spend cap** on the API key in the Anthropic console.
- Per-agent token logging → surface monthly cost per agent on the metrics dashboard (§11).
- Pricing reference: https://platform.claude.com/docs/en/about-claude/pricing

**Eval set requirement:** persist ~30 real conversation transcripts (anonymized) as a fixture set. Any model-routing change (e.g., testing Haiku vs. Sonnet on the Conversation Agent) must be justified by rerunning the fixtures on both models and comparing tool-call accuracy and answer quality. Store results in `evals/` with date + model IDs. No routing changes on vibes.

---

## 10. Build Phases

**Phase 1 — Core planner (ship to first users):**
OTP + static GTFS; nearest-stop; chat agent with `plan_route`; subway GTFS-RT live predictions in answers; PWA shell. *No probabilities yet.*

**Phase 2 — Risk Engine:**
subwaydata.nyc backfill (start with 60–90 days); aggregate pipeline; `get_risk` tool; provenance UI; start subway + Q70/M60 collectors day one of Phase 1 so bus data matures in parallel.

**Phase 3 — Monitoring:**
Monitored trips, TTL/geofence/dismiss lifecycle, Re-plan Agent, on-return notices, deadline mode.

**Phase 4 — Self-operation:**
Feedback triage lanes, coverage-expansion loop, email opt-in, GitHub Issue filing, weekly digest.

**Phase 5 — Airport mode & polish (data-gated):**
Activate LGA airport mode when Q70/M60 buckets cross `n≥200`; accessibility mode, web push, airport knowledge base (terminal stops, security buffers), metrics dashboard. Evaluate JFK activation based on demand signals from the coverage-expansion queue + subway-leg data sufficiency.

---

## 11. Metrics (instrument from day one)

- WAU / returning users; monitored trips created & completed.
- Prediction calibration: predicted p_miss vs. observed outcomes (log it — this is the headline credibility metric).
- % feedback resolved with zero human touch; coverage requests → median days to live.
- Notification → return-visit conversion.
- LLM cost per agent per month; LLM calls per monitored trip (target: ≤3); % of notifications served by template vs. LLM.

---

## 12. Non-Goals & Guardrails

- No LLM-generated numbers, ETAs, or routes — tool outputs only.
- No native apps, no fare logic, no commuter rail in v1.
- No dark-pattern permission prompts; push asked only in context.
- Feedback lane 1 is the *only* autonomous write path, and only to corridor config.
