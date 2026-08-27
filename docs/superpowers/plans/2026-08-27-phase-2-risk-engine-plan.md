# Phase 2 — Risk Engine: Implementation Plan

Spec: `is-my-train-screwed-spec.md` §5, §5.1, §5.2. Supersedes the one-paragraph Phase 2 sketch in `docs/superpowers/plans/2026-08-14-is-my-train-screwed-roadmap.md` (lines 1004-1009) — that sketch predates Phase 1's actual build and assumed a live subway GTFS-RT collector using `nyct-gtfs`, which turned out to be unnecessary (see "Architecture deviation from the original roadmap" below).

**All open decisions below are resolved (user, 2026-08-27).** This plan is ready to execute.

## What's already true, verified live 2026-08-27 (don't re-derive)

- **Bus raw data has been collecting since 2026-08-15** (`backend/collectors/bus_collector.py`, deployed on Railway). 12 days on hand as of today. Real volume checked directly against the latest local backup: ~570K-695K raw poll records/day at the original 30s cadence, both corridors present in healthy volume (one sampled day: 499,086 `M60+` records, 117,404 `Q70+` records — these are raw per-poll-cycle snapshots, not yet arrival events; real event counts after derivation will be far smaller but comfortably above the `n≥200` threshold per bucket once aggregated). Schema exactly matches spec §5.2's raw snapshot shape already.
- **Collector polling interval changed 30s → 60s same day (commit `b1fbe64` on `main`), already live.** Roughly halves ongoing storage/bandwidth while staying well under spec §5.2's 90s ambiguity threshold for passage-moment derivation — 60s leaves real margin against jitter/retries; 90s exactly would leave none. All data collected before this change remains valid (a coarser gap doesn't invalidate a `clean` derivation, it just makes a few more borderline cases `ambiguous`).
- **subwaydata.nyc is confirmed still live and still updating daily** (checked through 2026-08-26, yesterday) at the exact URL pattern the spec documents (`subwaydata.nyc/data/subwaydatanyc_{date}_csv.tar.xz`) — it 302-redirects to a hash-suffixed CDN URL; any redirect-following HTTP client (already the pattern in `load_static_gtfs.py`) handles this transparently, no hash-discovery logic needed.
- **subwaydata.nyc's actual CSV schema, inspected directly** (downloaded and unpacked a real day, 2026-08-24): two files per day, `trips.csv` (8,582 rows) and `stop_times.csv` (229,879 rows). This is **already-derived observation data**, not raw polling — `stop_times.csv` has `trip_uid, stop_id, track, arrival_time, departure_time, last_observed, marked_past` with real Unix-epoch timestamps; `trips.csv` has `trip_uid, trip_id, route_id, direction_id, start_time, vehicle_id, last_observed, marked_past, num_updates, num_schedule_changes, num_schedule_rewrites`. subwaydata.nyc has already done the "approaching→past" state-machine work bus still needs — subway's ingestion task is an import/reformat job, not a new state machine.
- **subwaydata.nyc's own `trip_id` field is the same short suffix format** (`024000_7..S`) as MTA's live GTFS-RT trip_id — no schedule-prefix. This means **`backend/app/realtime_proxy.py`'s `TripIndex` class (built in Phase 1 Task 11) is directly reusable** for matching subwaydata.nyc rows back to static GTFS trip_id, to compute `scheduled_arrival_ts`/`delay_seconds`. Real, verified code reuse, not a guess.
- **subwaydata.nyc already covers all subway routes citywide, including M** — confirming an M→Q70 transfer needs no special collection step. Per spec §5, transfers are never stored as pairs; `get_risk` composes any transfer (M→Q70 included) at query time from each route's independent bucket. M's data flows in through Task 3 (subway import) automatically once built; Q70's already flows in through the existing bus collector. Nothing route-specific to add to either collector for this pairing.
- **No database exists yet anywhere in this project.** Phase 1 built everything file-based or stateless. Phase 2 is the first work that needs real persistent storage.
- **Anthropic spend cap is now set** ($5/month, confirmed by the user 2026-08-27) — this unblocks Task 6's Step 7 from Phase 1 (deferred: wiring a live `/chat` endpoint), folded into this plan as Task 8 below.

## Architecture deviation from the original roadmap (flagging, not silently following)

The pre-Phase-1 roadmap sketch assumed Phase 2 needed a dedicated live subway GTFS-RT collector (using `nyct-gtfs` to decode the NYCT protobuf extension) feeding `arrival_events` directly from live polling, mirroring the bus collector's design. **That's not needed.** Phase 1 already built live subway GTFS-RT data flowing through OTP (Tasks 9 and 11's real-time updaters + trip-id rewriting proxy). Combined with subwaydata.nyc's daily historical archive:

- **Live center** for spec §5's "live+historical blend" ("live GTFS-RT prediction sets the distribution center") comes from OTP's already-built real-time-enhanced routing responses at query time — no separate collector needed.
- **Historical spread** (the variance/distribution shape) comes from `reliability_buckets`, built from subwaydata.nyc's daily archive (an import job) plus the bus collector's raw polling (a real state-machine derivation, since bus has no equivalent pre-processed archive).

Net effect: **no new subway collector process to build or deploy.** This significantly shrinks Phase 2's scope versus the original sketch. Bus still needs its own arrival-event derivation (the state machine) since nothing does that upstream for bus the way subwaydata.nyc does for subway.

## Decisions (resolved 2026-08-27)

1. **DB approach: raw `sqlite3`** (stdlib, no new dependency) + Pydantic models at the read/write boundary. No SQLAlchemy.
2. **subwaydata.nyc backfill window: 90 days.** ~135MB total, trivial.
3. **Nightly aggregation: manual/on-demand for now** — a script you run yourself, not a scheduled service. Automate later if it proves annoying.
4. **Deploy the backend as part of Phase 2** — OTP + the FastAPI app + the new DB all need to go live somewhere (currently only the bus collector is deployed). Folded in as Task 10 below.
5. **Future corridors beyond Q70/M60 are on hold** pending your own survey of common rider routes — don't add speculative corridors; the collector stays scoped to the current two until you bring back specific ones to add.
6. **`StopIndex` gets wired up** — folded in as Task 9 below.

## Task breakdown

### Task 1: SQLite schema + connection layer
- New: `backend/db.py` — schema creation (`arrival_events`, `reliability_buckets` tables, exact columns per spec §5.2), a connection-management helper. Raw `sqlite3`, Pydantic models at the boundary.

### Task 2: subwaydata.nyc backfill downloader
- New: `backend/scripts/download_subwaydata.py`, matching `load_static_gtfs.py`'s existing conventions (verify-then-download, clear per-file logging).
- Downloads 90 days into `backend/data/raw/subway/` (gitignored, mirrors `backend/data/raw/bus/`'s existing layout).

### Task 3: Subway arrival-event import (reformat, not a new state machine)
- New: `backend/scripts/ingest_subwaydata.py`.
- For each downloaded day: read `trips.csv`+`stop_times.csv`, match each `trip_id` to the static GTFS trip_id via `TripIndex` (imported from `app.realtime_proxy`, reused as-is), compute `delay_seconds` (observed vs. scheduled), and write rows into the `arrival_events` table (Task 1's schema) with `derivation_quality='clean'` for matched rows.
- Unmatched trip_ids (the ~10-25% that don't suffix-match, per Task 11's measured rates) get skipped, not fabricated — consistent with the project's "never fabricate an observation" rule.

### Task 4: Bus arrival-event derivation (the real state machine)
- New: `backend/scripts/derive_bus_arrival_events.py`.
- Processes `backend/data/raw/bus/*.ndjson[.gz]` (already collecting, 12 days ready right now, now at the new 60s cadence going forward) per spec §5.2's exact rules: per-vehicle "approaching→past" state machine, dedupe by `(vehicle_id, stop_id, service_date, ~window)`, `derivation_quality='ambiguous'` when a polling gap >90s straddles the passage moment, never fabricate from schedule.
- This is the one genuinely new algorithm in Phase 2 — no existing code to reuse, unlike Tasks 3/6.

### Task 5: Aggregation script (manual/on-demand, per decision #3)
- New: `backend/scripts/aggregate_reliability_buckets.py`.
- Folds a day's `arrival_events` into `reliability_buckets` per `(agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type)`, exponential decay (`hist = 0.95·hist + 0.05·yesterday`, `n` decayed the same way per spec §5.2). Run by hand for now.

### Task 6: `get_risk` — the Risk Engine query-time function
- New: `backend/app/risk_engine.py` — pure function, no LLM, per spec §5's query-time contract: identify transfer points within an `Itinerary` (where one `Leg` ends and another begins at the same/nearby stop), fetch incoming-leg arrival distribution + outgoing-leg headway distribution from `reliability_buckets`, Monte Carlo (~1000 draws) → `{p_miss, n, window_days, quality}`. `quality: "insufficient"` when `n < 200` for any required bucket, per spec.

### Task 7: Wire `get_risk` into the Conversation Agent
- New tool schema (`GET_RISK_TOOL`, matching `PLAN_ROUTE_TOOL`'s existing pattern in `app/agents/tools.py`).
- `ConversationAgent` calls it after `plan_route`, narrates the returned `p_miss`/`n`/`window_days` — never computes or estimates a number itself (existing hard rule, already enforced in the system prompt).

### Task 8: Live `/chat` endpoint (Phase 1's deferred Step 7, now unblocked)
- New: `POST /chat` in `app/api/`, calling `ConversationAgent.respond`. This is the one piece of Phase 1's own plan that was explicitly left undone pending the spend cap — now confirmed set.
- One live manual smoke test end-to-end (real API call, real cost logged via `cost_guard`) before calling this done, per the original Task 6 brief's own Step 7 instruction.
- Frontend's `api/client.ts` already targets `/chat` with the right request/response shape (built in Phase 1 Task 7, never had a live endpoint to hit) — should need no frontend change, just needs a real endpoint to exist.

### Task 9: Wire up `StopIndex`
- Built in Phase 1 Task 3, zero callers since. `StopIndex.nearest(lat, lon, k)` takes coordinates, not free text — it can't resolve "near Roosevelt Island" from a text description on its own. Minimal real use: a `find_stop` tool (name/substring match against the already-loaded `stop_name` field) so the Conversation Agent can resolve a place name a user types (rather than requiring exact lat/lon) into a real stop for `plan_route`. Needs its own brief to nail down exact scope — flagging here rather than speculatively designing it in this planning doc.

### Task 10: Deploy backend + OTP + DB
- Currently only the bus collector is deployed (Railway). OTP runs locally via `docker compose`; the FastAPI app has never been deployed; there's no DB yet to deploy at all.
- Needs real research before a brief gets written (matching this project's own established discipline — verify live, don't assume): Railway's private networking for how a deployed OTP service reaches the trip-id rewriting proxy (production can't use `host.docker.internal`, that's a Docker-Desktop-local convenience only), how OTP's graph-build resource/time cost scales on Railway's infra (only ever tested locally), volume persistence for both OTP's graph data and the new SQLite file, and realistic hosting cost now that OTP is doing far more (7 static feeds + a 150MB OSM extract + a live rewriting proxy) than the trivial single-process bus collector Phase 1 already deployed.
- Sequence last, after Tasks 1-9 are built and locally verified — deploying broken/unverified code first would just move debugging onto a slower feedback loop.

### Task 11: Provenance UI
- Trip cards + probability badges with a provenance tooltip ("42 min, but 30% chance you miss the Q70 connection — based on 3,400 observed F arrivals, last 30 days") in the PWA.
- Should follow the frontend-design skill again before touching component code, same as Phase 1 Task 7 — this is genuinely new UI surface (trip cards didn't exist in Phase 1's minimal chat shell), not a tweak to what exists.
- Sequence last — depends on Task 8 (`/chat` live) and Tasks 6-7 (`get_risk` actually returning data) to be meaningfully testable end-to-end.

## Suggested execution approach

Same subagent-driven-development pattern as Phase 1: new isolated worktree, task briefs with complete code written just-in-time per task (matching how Phase 1's later tasks were briefed only once the preceding task's real state was known, not all upfront). Sequence: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 — each of 1-7 depends on the one before it (DB must exist before anything writes to it; arrival events before aggregation; aggregation before `get_risk` has anything to query). 8 and 9 are independent of 1-7 and could in principle run earlier, but are sequenced after to keep the Risk Engine's core data pipeline as the main thread. 10 and 11 both depend on everything before them being real and locally verified.
