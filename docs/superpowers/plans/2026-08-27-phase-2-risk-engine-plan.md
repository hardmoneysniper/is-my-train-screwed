# Phase 2 — Risk Engine: Implementation Plan

Spec: `is-my-train-screwed-spec.md` §5, §5.1, §5.2. Supersedes the Phase 2 sketch in `docs/superpowers/plans/2026-08-14-is-my-train-screwed-roadmap.md` (lines 1004-1009), written before Phase 1 existed and wrong about one thing — see "Deviation" below.

All decisions below are resolved (user, 2026-08-27). Ready to execute.

## Verified facts

- Bus collector has run since 2026-08-15 (`backend/collectors/bus_collector.py`, Railway). 12 days on hand. Volume at the original 30s cadence: ~570-695K raw records/day (one sampled day: 499,086 `M60+`, 117,404 `Q70+` — raw per-poll snapshots, not arrival events; real event counts after derivation will be far smaller, comfortably above `n≥200` per bucket).
- Interval changed 30s→60s (commit `b1fbe64`, live and confirmed via Railway's API and its own runtime log). Halves ongoing volume; stays well under spec §5.2's 90s ambiguity threshold with real margin (90s exactly would leave none). Data collected before the change is unaffected.
- subwaydata.nyc: live through 2026-08-26 at the documented URL pattern (302-redirects to a hashed CDN URL; any redirect-following client handles it, no special logic needed).
- subwaydata.nyc's real schema (inspected 2026-08-24's archive): `trips.csv` (8,582 rows/day) + `stop_times.csv` (229,879 rows/day), **already-derived observation data** — real Unix-epoch arrival/departure timestamps, not raw polling. Subway's ingestion task is an import job, not a new state machine.
- subwaydata.nyc's `trip_id` is the same short suffix format (`024000_7..S`) as live MTA GTFS-RT. `TripIndex` (`backend/app/realtime_proxy.py`, built Task 11) matches it to static GTFS trip_id as-is — reused, not rebuilt.
- subwaydata.nyc is citywide, not corridor-filtered — M is already in it. An M→Q70 transfer needs no collector change: spec §5 never stores transfer pairs, `get_risk` composes any pair at query time from independent per-route buckets.
- No database exists anywhere in this project yet.
- Anthropic spend cap is set ($5/month, confirmed 2026-08-27) — unblocks Task 8.
- Railway supports scheduled ("cron") services natively (`cronSchedule` field, confirmed via its API) — this is where Task 5 runs once Task 10 deploys the backend.

## Deviation from the pre-Phase-1 sketch

The original sketch assumed a live subway GTFS-RT collector (`nyct-gtfs`, decoding the NYCT protobuf extension) feeding `arrival_events` directly, mirroring bus. Not needed: Phase 1 already put live subway GTFS-RT through OTP (Tasks 9, 11). The live-center half of spec §5's "live+historical blend" comes from that; the historical-spread half comes from `reliability_buckets`, built from subwaydata.nyc (subway) and the bus collector (bus, via a real derivation — nothing pre-processes bus the way subwaydata.nyc does subway). **No new subway collector to build or deploy.**

## Decisions

1. DB: raw `sqlite3`, no ORM. Pydantic models at the read/write boundary.
2. subwaydata.nyc backfill: 90 days (~135MB).
3. Nightly aggregation runs on a schedule (Railway cron), not by hand — see Task 5/10.
4. Backend (FastAPI + OTP + DB) deploys as part of this phase.
5. No new corridors beyond Q70/M60 until the user's own rider survey identifies specific ones.
6. `StopIndex` gets wired up.

## Tasks

**1 — SQLite schema.** `backend/db.py`: `arrival_events` + `reliability_buckets` tables per spec §5.2, connection helper.

**2 — subwaydata.nyc downloader.** `backend/scripts/download_subwaydata.py`, matching `load_static_gtfs.py`'s conventions. 90 days into `backend/data/raw/subway/` (gitignored).

**3 — Subway arrival-event import.** `backend/scripts/ingest_subwaydata.py`. Per day: read `trips.csv`+`stop_times.csv`, match `trip_id` via `TripIndex`, compute `delay_seconds`, write to `arrival_events` (`derivation_quality='clean'`). Unmatched trip_ids (~10-25%, per Task 11's measured rates) are skipped, not fabricated.

**4 — Bus arrival-event derivation.** `backend/scripts/derive_bus_arrival_events.py`. The one genuinely new algorithm here: per-vehicle approaching→past state machine over `backend/data/raw/bus/*.ndjson[.gz]`, per spec §5.2 exactly — dedupe by `(vehicle_id, stop_id, service_date, ~window)`, `derivation_quality='ambiguous'` on a >90s polling gap, never fabricate from schedule.

**5 — Nightly aggregation.** `backend/scripts/aggregate_reliability_buckets.py`. Folds a day's `arrival_events` into `reliability_buckets` per `(agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type)`, exponential decay (`hist = 0.95·hist + 0.05·yesterday`, `n` decayed the same way). Runs as a Railway cron service (Task 10 deploys it), not manually.

**6 — `get_risk`.** `backend/app/risk_engine.py` — pure function, no LLM. Finds transfer points in an `Itinerary`, pulls incoming-arrival + outgoing-headway distributions from `reliability_buckets`, Monte Carlo (~1000 draws) → `{p_miss, n, window_days, quality}`. `quality: "insufficient"` when `n < 200`.

**7 — Wire `get_risk` into the Conversation Agent.** New `GET_RISK_TOOL` (matches `PLAN_ROUTE_TOOL`'s existing pattern). Agent calls it after `plan_route`, narrates the result, computes nothing itself.

**8 — Live `/chat` endpoint.** Phase 1's Task 6 Step 7, deferred pending the spend cap — now unblocked. `POST /chat` in `app/api/`, calling `ConversationAgent.respond`. One live smoke test (real call, real cost logged) before this counts as done. Frontend already targets `/chat` with the right shape (Phase 1 Task 7) — should need no frontend change.

**9 — Wire up `StopIndex`.** Built Task 3, zero callers since. It takes coordinates, not text, so it can't resolve "near Roosevelt Island" on its own. Minimal real use: a `find_stop` tool doing name/substring match on the already-loaded `stop_name` field, so the agent can resolve a typed place name instead of requiring exact lat/lon. Scope to be nailed down at brief-writing time.

**10 — Deploy backend + OTP + DB + the aggregation cron.** Only the bus collector is deployed today; OTP is local-only (`docker compose`); there's no DB to deploy yet. Needs real research before a brief exists, not assumptions: Railway's private networking for OTP↔proxy (production can't use `host.docker.internal`, that's Docker-Desktop-local only), OTP's build cost/time on Railway's infra at its current size (7 static feeds + 150MB OSM extract + a live proxy — far more than the trivial single-process collector already deployed), volume persistence for OTP's graph data and the SQLite file, and realistic hosting cost at this new scale. Sequenced last — deploying before Tasks 1-9 are locally verified just moves debugging onto a slower loop.

**11 — Provenance UI.** Trip cards + probability badges + tooltip ("42 min, but 30% chance you miss the Q70 connection — 3,400 observed F arrivals, last 30 days"). Follow the frontend-design skill first, same as Phase 1 Task 7 — this is new UI surface, not a tweak. Needs Task 8 (`/chat` live) and Tasks 6-7 (`get_risk` returning real data) to be testable end-to-end, so it's sequenced last.

## Execution

Same pattern as Phase 1: isolated worktree, subagent-driven, task briefs written just-in-time (not all upfront) so each reflects the prior task's real state. Order: 1→2→3→4→5→6→7→8→9→10→11. 1-7 are a strict dependency chain (DB before writes, events before aggregation, aggregation before `get_risk` has anything to query). 8-9 don't depend on 1-7 but are sequenced after to keep the Risk Engine pipeline as the main thread. 10-11 need everything before them built and locally verified first.
