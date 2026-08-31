# Phase 3 — Monitoring: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task, matching Phase 2's proven pattern — task briefs written just-in-time (not all upfront) so each reflects the prior task's real state, controller-verified facts embedded directly in the brief, not assumed.

**Goal:** Monitored trips that poll live MTA data, re-plan around real disruptions, and surface honestly via chat — no push, no polling, no fabricated numbers.

**Architecture:** An in-process 60s poll loop (Trip Monitor, pure code) detects triggers against live alerts + Phase 2's `reliability_buckets`; a triggered Re-plan Agent (LLM, template-first) re-queries `plan_route`/`get_risk` fresh and writes a pending notification; every `/chat` call atomically surfaces any pending notification before answering the user's actual message.

**Tech Stack:** Same as Phase 1/2 — FastAPI backend, raw sqlite3 (WAL mode, added this phase), Anthropic tool-calling (Haiku), React/Vite frontend. No new services, no new infrastructure.

Design doc: `docs/superpowers/specs/2026-08-31-phase-3-monitoring-design.md` — read it in full before writing any task brief; it resolves every open question this plan assumes as settled (notification delivery, discovery mechanism, geofencing scope, concurrency model, the reroute-changes-transfer-shape edge case). This plan doesn't re-derive those decisions, it builds against them.

## Global Constraints

- **LLM agents never compute numbers, routes, or probabilities.** Re-plan Agent narrates real `plan_route`/`get_risk` output only — same hard rule as the Conversation Agent (`CLAUDE.md`).
- **Model choice is a config/env parameter, never hardcoded.** Re-plan Agent's model comes from `settings`, default Haiku 4.5, same as `conversation_agent_model`.
- **Never fabricate.** A re-plan that can't verify a route/stop skips that trip's cycle, doesn't guess. A `get_risk` call that returns `[]` or `quality="insufficient"` never gets a citation footer invented for it.
- **Every displayed probability carries `n` and window** (spec §5) — the Re-plan Agent's citation format is the *same* convention as the Conversation Agent's (Phase 2 Task 11), shared from one place, not reimplemented.
- **Minimalism** (`CLAUDE.md`): no new Railway services, no push infrastructure, no polling endpoint, no queue table — all explicit design-doc decisions, not to be revisited without a real forcing constraint discovered during a task.
- **Spec §6 lifecycle, geofencing excluded per design doc:** termination is explicit action or TTL (`scheduled arrival + 30 min`) only.
- **Spec §9.1 cost envelope:** typical monitored trip = 0-3 LLM calls total. Re-plan Agent must default to string templates; Haiku only for genuine multi-option tradeoffs.

---

## Task 1 — `monitored_trips` schema + concurrency primitives

**Files:**
- Modify: `backend/db.py` (add `monitored_trips` table DDL to the existing schema-creation script; enable `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout` on `get_connection()`)
- Create: `backend/app/models/monitoring.py` (Pydantic models: `MonitoredTrip`)
- Test: `backend/tests/test_db.py` (extend), `backend/tests/test_monitoring_db.py` (new — concurrency-specific tests)

**Interfaces:**
- Produces: `monitored_trips` table exactly per the design doc's schema (`id, anonymous_id, itinerary_snapshot, deadline_ts, status, created_at, ttl_expires_at, last_checked_at, pending_notification`), plus indexes on `(anonymous_id)` and `(status, ttl_expires_at)`. Two reusable atomic-SQL helper functions in `db.py`: `claim_pending_notifications(conn, anonymous_id) -> list[dict]` (the `UPDATE ... RETURNING` from the design doc, atomic) and `claim_active_trips_for_polling(conn, staleness_seconds) -> list[dict]` (the poll-claim `UPDATE ... RETURNING`, atomic).
- Consumes: nothing new — extends Task 1's existing `get_connection()` pattern from Phase 2.

**Key decisions to implement exactly (from the design doc, don't re-derive):** WAL + busy_timeout on every connection, not just new ones — this is a `get_connection()`-level change, so it also retroactively protects Phase 2's aggregation loop. Both claim helpers must be single atomic statements (`UPDATE ... WHERE ... RETURNING`), never a separate SELECT followed by an UPDATE — that's the whole point, and it's exactly the kind of thing a review must trace by hand, not take on faith.

**Tests to write explicitly (per the user's direction — name the edge case, don't just say "test concurrency"):**
- Two concurrent `claim_pending_notifications` calls for the same `anonymous_id` with one pending row — assert exactly one caller gets it back, the other gets `[]`. Do this for real: two real connections/threads racing against the same real sqlite file in `tmp_path`, not a mocked assertion.
- Same concurrency proof for `claim_active_trips_for_polling` — two concurrent claims against the same stale trip, exactly one wins.
- A trip whose `last_checked_at` is recent (not stale) is correctly excluded from a claim.
- WAL mode is actually active on a connection from `get_connection()` (`PRAGMA journal_mode` returns `wal`, not the sqlite default `delete`).

---

## Task 2 — Anonymous ID plumbing

**Files:**
- Modify: `frontend/src/api/client.ts` (generate + persist a UUID in `localStorage` on first use; include it in every `/chat` request body)
- Modify: `backend/app/api/chat.py` (accept the new field on `ChatRequest`)
- Test: `frontend/src/App.test.tsx` (extend), `backend/tests/test_chat_api.py` (extend)

**Interfaces:**
- Produces: `ChatRequest.anonymous_id: str` (backend), a `getOrCreateAnonymousId(): string` helper (frontend) that later tasks' frontend code doesn't need to touch again.
- Consumes: nothing new.

**Key decisions:** Generate once, store in `localStorage` under one fixed key, reuse on every subsequent request from the same browser — never regenerate per session. If `localStorage` is unavailable (private browsing edge case), fall back to an in-memory id for that session rather than crashing — the user simply won't get cross-session monitoring continuity, which degrades honestly rather than breaking the chat entirely.

**Tests to write explicitly:**
- First call generates and persists an id; second call (same `localStorage`) reuses the same one.
- `localStorage` throwing (simulate `Storage` access denial) falls back to an in-memory id without crashing the send flow.
- Backend accepts and stores the id in whatever downstream calls receive it (a wiring test, not re-testing `localStorage` itself).

---

## Task 3 — Alerts feed client

**Files:**
- Create: `backend/app/alerts.py`
- Test: `backend/tests/test_alerts.py`

**Interfaces:**
- Produces: `async def fetch_subway_alerts() -> list[AlertRecord]` and `async def fetch_bus_alerts() -> list[AlertRecord]`, where `AlertRecord` (Pydantic, same file) has at minimum `route_id: str`, `stop_ids: list[str]`, `header_text: str`, `active: bool`. Both real, live endpoints — see below, don't re-derive.
- Consumes: nothing new.

**Verified live this session, use these exact URLs — don't re-search:**
- Subway: `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts` — no key required, real protobuf `FeedMessage` (confirmed HTTP 200, ~477KB real payload). Parse via `google.transit.gtfs_realtime_pb2` (already a dependency, used by `backend/app/realtime_proxy.py` — read that file for the parsing pattern to follow) — alerts are the `entity.alert` field, not `entity.trip_update`.
- Bus: `https://gtfsrt.prod.obanyc.com/alerts?key={MTA_BUSTIME_API_KEY}` — same base URL family as the existing bus collector's `tripUpdates`/`vehiclePositions` endpoints (`backend/collectors/bus_collector.py`), confirmed live in Phase 1 kickoff. Same protobuf parsing pattern, `entity.alert`.

**Key decisions:** This module fetches fresh on every call — no persistent collector, no raw-ndjson archival (unlike the bus reliability collector). Alerts are current-state, not something Phase 3 needs historical stats on. A feed fetch failure raises (matching `realtime_proxy.py`'s existing style) — the *caller* (Task 7's Trip Monitor loop) is responsible for catching and skipping gracefully per the design doc's error-handling section, this module itself doesn't swallow errors.

**Tests to write explicitly:**
- A real sample protobuf `FeedMessage` (construct one in the test, don't need network) with a mix of alert and non-alert entities parses into the correct `AlertRecord` list, ignoring non-alert entities.
- An alert entity with no `informed_entity` (malformed/edge-case protobuf) doesn't crash parsing — skipped, not fabricated into an empty-route alert.
- HTTP failure (mocked 500) propagates as an exception, isn't swallowed here.

---

## Task 4 — Deadline-mode backward-planning helper

**Files:**
- Create: `backend/app/deadline.py`
- Test: `backend/tests/test_deadline.py`

**Interfaces:**
- Produces: `def compute_deadline_threshold(itinerary: Itinerary, deadline_ts: int, conn: sqlite3.Connection | None = None, route_index: RouteIndex | None = None) -> int | None` — returns the epoch-ms timestamp the trip must depart by (`deadline_ts - p85_travel_time`), or `None` if insufficient data exists for the route (mirrors `get_risk`'s own `quality="insufficient"` honesty — never returns a threshold computed from partial/fabricated data). Same `conn`/`route_index` injection convention as `risk_engine.get_risk` (Task 6, Phase 2) for testability.
- Consumes: `Itinerary`/`Leg` (`backend/app/models/transit.py`), `reliability_buckets` (via `db.get_connection`), `RouteIndex`/feed-prefix-stripping helpers already built in `backend/app/risk_engine.py` and `backend/app/route_index.py` — reuse them, don't reimplement route/stop resolution a second time.

**Key decisions:** p85 = the 85th-percentile value of the relevant route's travel-time-variance histogram, computed as a cumulative-sum-over-bins on the same `{bin_width_s, min_s, counts}` shape Phase 2 already established — no new statistical machinery. **Open question the task brief must resolve against the real current `backend/app/risk_engine.py` before writing any code, not guess from an earlier phase's memory:** this helper needs a *total travel-time* variance signal, which is conceptually different from `get_risk`'s *transfer-miss* signal (`_incoming_stat_type`'s subway-gets-`delay`/bus-gets-`prediction_error` selection was solving a different problem — the incoming leg's arrival-time spread at a transfer point, not a whole trip's duration variance). Read `risk_engine.py`'s current stat-selection logic firsthand, then decide deliberately whether this helper reuses the same per-agency stat choice or needs its own — don't assume they're the same thing just because the histogram shape is shared.

Also decide and record explicitly: is the "route" here a single leg's stat, or does a multi-leg itinerary need its variance summed/combined across legs (e.g. two independent legs' p85s don't simply add — combining distributions correctly, or deliberately approximating by summing each leg's own p85 as a conservative upper bound, are both defensible; picking one without stating it explicitly is the kind of silent gap this design already got caught on once this session).

**Tests to write explicitly:**
- A route with a real, sufficient histogram produces a correct p85-derived threshold (hand-computable expected value from a small synthetic histogram).
- A route with no bucket at all, or `n < 200`, returns `None` — never a threshold computed from thin data.
- The percentile calculation is correct at a bin boundary (an edge case worth naming, not just "test the happy path").

---

## Task 5 — `create_monitored_trip` / `cancel_monitored_trip` tools + Conversation Agent wiring

**Files:**
- Modify: `backend/app/agents/tools.py` (add `CREATE_MONITORED_TRIP_TOOL`, `CANCEL_MONITORED_TRIP_TOOL`)
- Modify: `backend/app/agents/conversation_agent.py` (dispatch branches, `SYSTEM_PROMPT` extension for the proactive-offer + deadline-extraction behavior)
- Test: `backend/tests/test_conversation_agent.py` (extend)

**Interfaces:**
- Consumes: `get_risk` (real 3-arg signature per Phase 2), `claim_active_trips_for_polling`/schema from Task 1, `compute_deadline_threshold` from Task 4.
- Produces: `create_monitored_trip(itinerary: Itinerary, anonymous_id: str, deadline_ts: int | None, conn=None) -> int` (returns the new trip's id), `cancel_monitored_trip(trip_id: int, anonymous_id: str, conn=None) -> bool`.

**Key decisions (from the design doc — implement exactly, this is the part most likely to be gotten wrong by a fresh implementer guessing):** the agent offers monitoring when `get_risk` returned any entry with `quality="ok"` (any real `p_miss`, no magnitude threshold) OR the trip reads as deadline-sensitive — never for a `[]` result with no deadline signal. Deadline extraction (parsing a time from conversation, recognizing airport/flight/interview language) is an LLM classification/parsing task, not a computed number — stays in the system prompt, not new Python logic. `cancel_monitored_trip` must verify `anonymous_id` matches the trip's owner before cancelling — never let one browser's id cancel another's trip.

**Spec §6's "explicit action" termination path lands here too, not in Task 7:** the `SYSTEM_PROMPT` extension must also instruct the agent to recognize conversational cancel/arrival intent ("I'm here," "cancel this trip," "I made it") for a trip the user is actively discussing, and call `cancel_monitored_trip` accordingly — the same tool-calling pattern every other tool already uses, no separate mechanism. If the user's `anonymous_id` has more than one active trip and the conversational reference is ambiguous, the agent asks which one rather than guessing (matches this product's existing "ask clarifying questions" pattern from the spec's own agent table, §3.2).

**Tests to write explicitly:**
- Zero-transfer trip with no deadline signal → agent does not offer monitoring (tool never called).
- Trip with a real `p_miss` → agent offers, user says yes → `create_monitored_trip` called with the real itinerary object (not LLM-reconstructed — same `is`-identity test pattern Task 7/Phase 2 used for `get_risk`).
- Deadline-sensitive zero-transfer trip → agent offers based on deadline alone, not risk.
- `cancel_monitored_trip` with a mismatched `anonymous_id` fails, doesn't cancel.
- An `anonymous_id` with two active trips and an ambiguous "cancel it" message → agent asks which trip, does not guess or cancel either one unprompted.

---

## Task 6 — Re-plan Agent

**Files:**
- Create: `backend/app/agents/replan_agent.py`
- Modify: `backend/app/agents/conversation_agent.py` (extract the citation-format text into a shared constant both this file and `replan_agent.py` import — don't duplicate the literal format string)
- Test: `backend/tests/test_replan_agent.py`

**Interfaces:**
- Produces: `async def replan_trip(trip: MonitoredTrip, trigger_reason: str, conn=None) -> str | None` (returns the notification text to store in `pending_notification`, or `None` if nothing needs saying — e.g. a false-alarm trigger that resolves to no real change).
- Consumes: `plan_route` (OTP client), `get_risk`, `compute_deadline_threshold` (Task 4), the shared citation-format constant.

**Key decisions (this is where the reroute edge case from the design doc lands — implement its three-part fix exactly, don't just re-query and stop):**
1. Re-query `plan_route` + `get_risk` fresh for the remaining leg(s) — unconditional, regardless of whether the original itinerary had transfers.
2. Update the trip's `itinerary_snapshot` in the DB to the new itinerary — every successful re-plan, not just ones that change the route shape.
3. If `trip.deadline_ts` is set, recompute the deadline threshold from the *new* itinerary via `compute_deadline_threshold` (Task 4) — never leave it pinned to the old route's distribution.
4. If the new plan has a `quality="ok"` transfer, the notification text uses the shared citation format (`%*` + footer with the real `n`/`window_days`) — if not, a plain template with no citation.
5. Template-first: a small fixed set of message templates cover the common cases (route disrupted, no transfer risk / route disrupted, new transfer with citation / trip proceeding fine, dismiss trigger). Haiku only invoked when the situation involves comparing multiple real alternative routes and a template can't express the tradeoff honestly — and even then, narrates real tool output only, same hard rule as everywhere else.

**Tests to write explicitly (the reroute edge case gets its own named test, not folded into a generic "re-plan works" test):**
- **Zero-transfer trip reroutes into a trip with a real transfer risk** → resulting notification text contains the citation format (`%*` + footer with real `n`/`window_days`), and the trip's `itinerary_snapshot` in the DB is updated to the new (transfer-having) itinerary.
- **Transfer trip reroutes into a zero-transfer trip** → notification has no citation (nothing to cite), `itinerary_snapshot` still updates.
- **Deadline-mode trip reroutes** → the stored deadline threshold changes to reflect the new route's own distribution (assert the new value differs from a hand-computed old-route value in a constructed scenario where they'd actually differ).
- A trigger that resolves to "still fine, no real change" → returns `None`, `pending_notification` isn't set to a manufactured message.
- Route/stop that no longer resolves against static GTFS → skipped honestly (`None`), doesn't crash.

---

## Task 7 — Trip Monitor poll loop

**Files:**
- Modify: `backend/app/main.py` (new `asyncio` background task in `lifespan`, alongside the existing aggregation loop — same pattern, separate task)
- Create: `backend/app/trip_monitor.py`
- Test: `backend/tests/test_trip_monitor.py`

**Interfaces:**
- Produces: `async def run_monitor_cycle(conn=None) -> dict` (one poll cycle: claim trips, check each, fire re-plans, sweep TTL — returns a summary dict for logging, matching the aggregation loop's existing log-line convention).
- Consumes: `claim_active_trips_for_polling` (Task 1), `fetch_subway_alerts`/`fetch_bus_alerts` (Task 3), `replan_trip` (Task 6), `reliability_buckets` for headway-anomaly comparison.

**Key decisions:** Fetch each alerts feed once per cycle (not once per trip), share the result across all trips checking overlapping routes. Each trip's check-and-maybe-update is its own short DB transaction — never one transaction spanning the whole cycle's trips (design doc's concurrency section, Task 1's atomic-claim tests already prove the primitive works; this task proves the *loop* uses it correctly, a different thing). TTL sweep (`ttl_expires_at` passed) sets `status='expired'` silently, no `replan_trip` call at all — spec §6 says this is silent cleanup, not a notification event.

**Tests to write explicitly:**
- A trip with a real alert on one of its segments triggers `replan_trip` (mocked) exactly once per cycle, not once per matching alert entity.
- A trip past its `ttl_expires_at` is marked `expired` and `replan_trip` is never called for it.
- Two trips on the same route share one alerts-feed fetch (assert the fetch mock is called once, not twice, for a two-trip cycle).
- A trip whose alerts check raises (network failure) doesn't prevent other trips in the same cycle from being checked — per-trip isolation, construct a cycle with one failing and one healthy trip, assert the healthy one still gets processed.
- Re-run the exact concurrency test from Task 1 at this layer too: two concurrent calls to `run_monitor_cycle` against the same trip only result in one `replan_trip` call, not two (this is the whole reason the claim primitive exists — prove the loop actually benefits from it, not just that the primitive works in isolation).

---

## Task 8 — Surface pending notifications on `/chat`

**Files:**
- Modify: `backend/app/agents/conversation_agent.py` (`respond()` — claim + prepend before generating the actual reply)
- Modify: `backend/app/api/chat.py` (pass `anonymous_id` through)
- Test: `backend/tests/test_conversation_agent.py` (extend), `backend/tests/test_chat_api.py` (extend)

**Interfaces:**
- Consumes: `claim_pending_notifications` (Task 1), `ChatRequest.anonymous_id` (Task 2).
- Produces: no new public interface — this is the wiring task connecting Tasks 1/2/6's outputs to what the user actually sees.

**Key decisions:** Claimed notifications are prepended as plain narrated text before the agent's own answer to the user's actual message — not a separate API field, not a second message bubble (matches the design doc's "no separate feedback window" framing, same principle as spec §8). If a claimed notification already contains the citation format (from Task 6), it must render correctly through the *existing* `splitFooter` frontend logic (Phase 2 Task 11) unmodified — this task should not need any frontend change at all; if it turns out to need one, that's a signal the citation format isn't being composed correctly upstream, not a reason to patch the frontend.

**Tests to write explicitly:**
- A user with one pending notification sends an unrelated message → response begins with the notification, then answers the actual question.
- A user with no pending notifications → response is unchanged from Phase 2 behavior (no regression, direct comparison against the pre-Phase-3 test fixtures).
- A user with multiple pending notifications (two different monitored trips both fired) → both are surfaced, not just one silently dropped.
- Claiming is atomic end-to-end through this endpoint — a notification is never shown twice across two rapid successive `/chat` calls (an integration-level version of Task 1's unit-level concurrency proof).

---

## Task 9 — Deploy + live end-to-end verification

**Files:** none new — this task ships Tasks 1-8's code to the already-live Railway `backend` service (no new services, no new OTP work — Phase 2's deployment already exists and this phase adds no new external infra per the design doc).

**What "done" means for this task:** matching Phase 2 Task 8's precedent — a real, live smoke test is the actual completion gate, not just green CI.
1. Deploy the merged code to the existing `backend` Railway service (`serviceInstanceDeploy`, `latestCommit: true`).
2. Real live call: plan a real trip with a transfer via `/chat`, accept the agent's monitoring offer, confirm a real row lands in `monitored_trips` (verify via `railway ssh` + a direct sqlite query against the deployed DB, not just trusting the HTTP response).
3. Real live call: manually flip that trip's `ttl_expires_at` to the past (direct DB write via `railway ssh`) and confirm the *next* poll cycle (wait for one real 60s cycle, checked via deployment logs) marks it `expired`, not stuck `active`.
4. Real live call: send a follow-up `/chat` message and confirm no stray notification appears for the now-expired trip (TTL termination is silent, per spec §6 — this is a real behavioral assertion, not just "it deployed").
5. Document the real numbers/log lines observed in `CLAUDE.md`'s deployment section, matching the existing documentation density for Phase 2's deploy — this phase didn't need new infrastructure, so the write-up is much shorter, but the live verification standard doesn't relax.

---

## Execution

Subagent-driven, same pattern as Phase 2: isolated worktree, task briefs written just-in-time by the controller (not all upfront), each brief embedding whatever live verification the task actually needs (e.g. Task 4's exact stat-selection question against the real current `risk_engine.py`, not guessed from this plan's own hedge). Order: 1→2→3→4→5→6→7→8→9. Tasks 2/3/4 have no dependency on each other and could be reordered or interleaved if that's more convenient at execution time — 1 must come first (everything else's DB access depends on it), 5/6/7/8 form the real dependency chain, 9 must come last.
