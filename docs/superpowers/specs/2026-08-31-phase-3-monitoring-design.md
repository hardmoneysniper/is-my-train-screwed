# Phase 3 — Monitoring: Design

Spec: `is-my-train-screwed-spec.md` §6 (Trip Monitoring Lifecycle), §7 (on-return notice mechanism, partially — see Scope below), §9.1 (Re-plan cost guidance), §10 (Phase 3 build-phase description).

## Position in the roadmap

Midpoint of the 5-phase v1 build:
- Phase 1 (Core planner) — done.
- Phase 2 (Risk Engine) — done, merged and live-deployed.
- **Phase 3 (Monitoring) — this document.**
- Phase 4 (Self-operation: feedback triage, coverage-expansion loop).
- Phase 5 (Airport mode & polish, data-gated).

Phase 3 is the first phase to introduce persistent background state (a monitored trip outlives the chat turn that created it) and a second, autonomously-triggered LLM agent (Re-plan), not just tool-calling within a user's own turn.

## Decisions resolved during brainstorming (2026-08-31, with the user)

1. **Notification delivery: in-app only.** Spec §6 says triggers should produce a "push notification," but spec §10 formally assigns web push delivery to Phase 5. Building real push now would pull Phase 5 work forward and duplicate effort. Phase 3 ships without any push infrastructure — a monitored trip's outcome surfaces the next time the user is in the chat, not while backgrounded. This is a real, acknowledged limitation (the product's own stated differentiator is proactively reaching someone *not* looking at their phone) that Phase 5 is expected to close.
2. **Discovery mechanism: surface on next `/chat` message only, no polling.** No new frontend polling loop or websocket. Every `/chat` call checks for pending notifications for the caller's `anonymous_id` before answering the user's actual message. Matches the product's existing "no separate feedback window" principle (spec §8) — chat is the one interaction surface, extended rather than duplicated.
3. **Geofencing: deferred.** Spec's own wording already treats it as conditional ("if location permission granted"). The other two termination paths (explicit action, TTL) fully cover trip termination on their own. Building geofencing now would add a new frontend permission flow for a nice-to-have precision improvement, not a functional requirement. Revisit if/when it's actually wanted.
4. **Starting a monitored trip: the agent offers proactively**, not only on explicit request — after `plan_route` + `get_risk`, if the itinerary has any transfer with `quality="ok"` (i.e. any real, stated `p_miss`, whatever its magnitude — no separate threshold; the number itself is the honest signal, same as Phase 2's citation format never suppresses a real number for being small), or the trip reads as deadline-sensitive, the agent asks whether to monitor it. A single-leg (zero-transfer) trip never has a risk percentage to point to (Phase 2's `get_risk` returns `[]` for it) — for those, only deadline-sensitivity can prompt the offer, never risk. If offering on every nonzero `p_miss` proves too naggy in practice, add a magnitude threshold later — not a hidden assumption baked in now.

## Architecture

```
Frontend: one addition — a persisted anonymous_id (generated once,
stored in localStorage), sent with every /chat request. No new UI
surfaces, no polling.

Backend, new components:
  ├─ Trip Monitor (pure code, no LLM) — in-process asyncio loop in the
  │   backend service, same pattern as Phase 2's nightly aggregation
  │   (Railway's per-service constraints already ruled out a separate
  │   service for this kind of background work — no shared volumes
  │   across services, and a cron-scheduled service there doesn't run
  │   persistently). Every 60s (spec §6): claims active trips (see
  │   Concurrency below), checks live alerts + trip updates for their
  │   segments, and a headway-anomaly check against Phase 2's own
  │   reliability_buckets (real data reuse, no new stat needed).
  ├─ Re-plan Agent (LLM, triggered only — not user-turn-triggered).
  │   Per spec §9.1: string-template first for the ~90% templatable
  │   case; Haiku only when comparing real multiple alternative routes
  │   needs actual judgment. Never invents a route or number — composes
  │   a message from real plan_route/get_risk output, same hard rule as
  │   every other agent in this product.
  └─ create_monitored_trip / cancel_monitored_trip tools, wired into
      the existing Conversation Agent's tool loop (same pattern as
      plan_route/get_risk/find_stop from Phase 2).
```

**Verified live during this design session (not assumed):** MTA's subway service-alerts GTFS-RT feed (`https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts`) is real and keyless — confirmed HTTP 200, genuine protobuf payload (477KB). Bus alerts were already confirmed live in Phase 1 kickoff (the same 3-endpoint BusTime check that covered tripUpdates/vehiclePositions).

## Data model & lifecycle

```sql
CREATE TABLE monitored_trips (
  id, anonymous_id, itinerary_snapshot JSON,  -- the real Itinerary from
                                               -- plan_route, frozen at
                                               -- creation
  deadline_ts NULLABLE,       -- only set for deadline-mode trips
  status TEXT,                -- 'active' | 'completed' | 'cancelled' | 'expired'
  created_at, ttl_expires_at, -- = scheduled arrival + 30 min (spec §6)
  last_checked_at NULLABLE,   -- poll-claim staleness marker, see Concurrency
  pending_notification TEXT NULLABLE  -- cleared atomically once surfaced
)
```

1. **Create:** `create_monitored_trip` → stores the snapshot, computes `ttl_expires_at`. If deadline mode (destination/language reads as airport/flight/interview-shaped, or the user says they're short on time), the Conversation Agent extracts the deadline time from conversation — a classification/parsing task, not a computed number, so it stays within the LLM's allowed scope — and a pure-code helper does the actual backward-planning: pulls the relevant route's real travel-time distribution from `reliability_buckets`, takes its p85, and monitors against `deadline_ts - p85_duration`, not just the route's own scheduled time. Reuses Phase 2's data directly.
2. **Poll (every 60s, Trip Monitor):** claim active trips (Concurrency, below) → check alerts feed for their segments' routes (fetched once per cycle, shared across all trips on the same route) → check headway anomaly against `reliability_buckets` → check TTL. A hit hands off to the Re-plan Agent.
3. **Re-plan fires:** re-query `plan_route` + `get_risk` for the remaining leg(s), decide template vs. Haiku, write the result into `pending_notification`.
4. **Terminate:** explicit action (`cancel_monitored_trip`, or the agent recognizing "I'm here"/"cancel" in conversation) → `status='completed'`/`'cancelled'`; or TTL passes → `status='expired'`, silent per spec §6 (no notification text — it just stops).
5. **Surface (on-return notice, same mechanism as any other notification — no separate infrastructure):** every `/chat` call, before answering the user's message, atomically claims any `pending_notification` rows for that `anonymous_id` (see Concurrency) and prepends them.

## Concurrency

The 60s Trip Monitor loop and every `/chat` request read/write the same table concurrently. SQLite has real, known sharp edges here (Phase 2 already hit a thread-affinity bug from carelessness in exactly this area) — designed explicitly rather than assumed away:

1. **`PRAGMA journal_mode=WAL`** on the connection (not currently set — Phase 2's DB access was batch jobs, never concurrent per-request reads/writes). Lets `/chat` reads proceed without blocking on the Trip Monitor's writes and vice versa.
2. **`PRAGMA busy_timeout`** alongside WAL — concurrent writers retry gracefully under contention instead of failing immediately with "database is locked."
3. **Atomic notification claim**, not read-then-write:
   ```sql
   UPDATE monitored_trips SET pending_notification = NULL
   WHERE anonymous_id = ? AND pending_notification IS NOT NULL
   RETURNING pending_notification, id;
   ```
   A naive select-then-clear has a race window where two concurrent requests (double-tap, two open tabs) could both read the same text before either clears it. One atomic statement closes it.
4. **Atomic poll-claim**, same technique, applied to the Trip Monitor's own "which trips do I check this cycle" query:
   ```sql
   UPDATE monitored_trips SET last_checked_at = ?
   WHERE status = 'active' AND (last_checked_at IS NULL OR last_checked_at < ?)
   RETURNING *;
   ```
   Today there is exactly one backend process, so this is a no-op protection. But it means the same polling code is safe to run from multiple `uvicorn` worker processes within one container (`--workers N`, a config-only change using CPU the current plan already pays for and isn't using) or multiple Railway replicas (the current plan's ceiling is 2) later, without a rewrite — each trip is only claimed by whichever racer's `UPDATE` lands first.
5. **One short transaction per trip in the poll loop, not one giant transaction for the whole cycle** — same discipline as the thread-affinity fix from Phase 2's aggregation loop, so one trip's slow/failing check doesn't hold a lock blocking every other trip's write or every concurrent `/chat` request for the whole cycle.
6. **Indexes** for the two hot access patterns: `(anonymous_id)` (every `/chat` call now does this lookup) and `(status, ttl_expires_at)` (the poll loop's active-trip query and TTL sweep).
7. **No queue table.** `pending_notification` stays one nullable column per trip, not a separate events/queue table — matches spec §6's own "0-3 LLM calls total" cost envelope (a low-volume signal, not a stream) and this project's minimalism rule.

**Retroactive check against what's already built (Phases 1-2), not just new code:** re-verified Phase 2's nightly aggregation transaction structure — the whole day's fold and its `processed_days` marker insert are one transaction, and that table's primary key means a second concurrent attempt at the same day fails its commit outright rather than partially applying. Already safe under multiple processes, just wasteful if it ever happened (redundant work attempted, not corrupted data) — no rework needed there. `get_stop_index()`/`RouteIndex`/`TripIndex` (Phases 1-2) are read-only in-memory caches, independently and safely rebuilt per process — also already the correct pattern.

**One honest gap, flagged not fixed:** `cost_guard.py`'s $5/month spend tracking (Phase 1) writes to a local file (`backend/data/cost_log.ndjson`) rather than the shared SQLite DB. Under multiple concurrent processes (extra `--workers`, or `numReplicas>1`), each would track its own spend blind to the others' — the combined real spend could exceed the cap while each process individually still believes it's under budget. Not worth fixing now (the product runs single-process today, and Railway's console-level hard cap remains the authoritative backstop per `CLAUDE.md`) — but if the backend is ever actually run with more than one process, move this from a file into a `cost_log` table in the same DB first, before anything else in this list becomes the bottleneck.

## Error handling

- **Alerts feed unreachable/malformed:** log and skip that poll cycle for the affected trips, retry next cycle. "Couldn't check" never silently means "nothing's wrong" in a way that cancels monitoring.
- **Re-plan Agent's Haiku call fails or hits the cost cap:** falls back to the template path if one applies; otherwise `pending_notification` simply isn't updated this cycle — no fabricated message, same "degrade honestly" principle as `get_risk`'s `n<200` path.
- **A monitored trip's stored itinerary snapshot references a stop/route that no longer resolves** (e.g. a service change between creation and a later poll): treated as "can't verify, skip" for that trip only — never crashes the whole poll cycle for other trips, guaranteed by the per-trip transaction isolation above.

## Testing

Matches this project's established convention: real SQLite (`tmp_path`) at the DB boundary, mocked HTTP for external feeds, no mocks of the database itself.

- Trip Monitor: synthetic `monitored_trips` rows, mocked alerts-feed responses. A direct concurrency test — two simulated concurrent claim attempts against the same trip, assert only one succeeds. TTL expiry. Per-trip isolation (one trip's bad data doesn't block others in the same cycle).
- Re-plan Agent: unit tests on the template-vs-Haiku decision boundary using real `TransferRisk`-shaped input; the Haiku path itself mocked, matching the Conversation Agent's existing test convention.
- `create_monitored_trip`/`cancel_monitored_trip`: same tool-dispatch test pattern already established for `get_risk`/`find_stop` (Phase 2, Tasks 7/9).
- At least one live smoke test once deployed (matching Phase 2 Task 8's precedent): create a real monitored trip, confirm it surfaces correctly on a subsequent real `/chat` call.

## Scaling note (why this matters now, not hypothetically)

Confirmed live during this design session: the backend's current plan allocates 8 vCPU / 8GB RAM, but its `startCommand` (`uvicorn app.main:app --host 0.0.0.0 --port 8000`) runs as a single process — one CPU core actually in use. If concurrent-client load ever became a real bottleneck (it is not expected to at Cornell Tech beachhead scale), the cheapest lever is `--workers N` on the same container (uses CPU already paid for, zero new Railway config) before reaching for `numReplicas` (true horizontal scaling, capped at 2 on the current plan) or a Postgres migration (spec §9's own noted path, relevant only for sustained heavy concurrent *write* traffic — SQLite serializes writers even under WAL). The concurrency design above (atomic claims, WAL, busy_timeout) is what makes `--workers N` or `numReplicas>1` a config change later rather than a redesign — multiple `uvicorn` workers in one container behave exactly like multiple replicas from the database's point of view, so this protects both scaling axes with the same code.

## Explicitly out of scope for Phase 3

- Web push delivery (Phase 5).
- Geofence-based trip termination (deferred, no target phase yet — revisit if wanted).
- Accessibility/luggage-aware routing signals feeding into monitoring triggers (Phase 5's accessibility mode, per spec §10).
- The full coverage-expansion loop's on-return notices for corridor-fulfillment (Phase 4) — Phase 3 builds the anonymous-ID + "surface on next message" mechanism as reusable infrastructure, but only wires it to monitoring events. Phase 4 reuses the same mechanism for coverage-request fulfillment; it doesn't need to rebuild it.
