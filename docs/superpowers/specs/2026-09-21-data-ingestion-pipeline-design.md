# Reliability Data Ingestion Pipeline — Design

Spec: `is-my-train-screwed-spec.md` §4/§5 (empirical, never-guessed reliability data is this product's core differentiator).

## Position in the project

Not a new feature — a production gap in already-shipped Phase 2 infrastructure. Higher priority than any of the phase/feature work brainstormed earlier this session (walking navigation, ferry routing extras): this is the thing that makes `get_risk` return real numbers instead of `"insufficient"` on every single query, which is the product's stated core value proposition.

## The gap, confirmed live this session

Checked the production database directly: `arrival_events` and `reliability_buckets` are both **completely empty** — not "still accumulating toward n≥200," zero rows, for both bus and subway. Three scripts exist from Phase 2 and are already correct and fully tested (`download_subwaydata.py`, `ingest_subwaydata.py`, `derive_bus_arrival_events.py` — each has its own passing test file), but none has ever been run against production data. This is a wiring gap, not a missing-feature gap.

**Subway**: `download_subwaydata.py` (90-day HTTP backfill from subwaydata.nyc) → `ingest_subwaydata.py` (parses into `arrival_events`, matching real-time trip_ids against static GTFS via the existing `TripIndex`). Both already write to/read from `backend/data/raw/subway/`, already on `backend`'s own Railway volume — no cross-service issue at all.

**Bus**: real data has been collecting continuously since mid-August (confirmed live: unbroken daily files through today, today's file already tens of MB and actively growing) — but on the **bus collector's own, separate Railway service volume**, not `backend`'s. `derive_bus_arrival_events.py` expects its input at `backend/data/raw/bus/`. Railway does not support sharing a volume across services — the exact same constraint that already forced Phase 2's nightly aggregator to run in-process on `backend` rather than as its own service.

**Both `download_subwaydata.run_backfill()`, `ingest_subwaydata.run_ingest()`, and `derive_bus_arrival_events.run_derive()` are already fully idempotent** — confirmed by reading the actual code, not assumed: each skips any service-date already present (`already_ingested`/`dest.exists()` checks), safe to call repeatedly.

## Architecture

### 1. Bus collector → in-process background task on `backend`

New async loop in `backend/app/main.py`, matching the exact existing lifespan-managed pattern (`_run_aggregation_loop`, Phase 3's trip monitor loop): wraps the collector's existing `run_forever()` (already a correct, working, `while True` sync poller with its own internal retry/backoff) via `asyncio.to_thread` — no changes to the collector's own logic. `bus_collector.py`'s `DATA_DIR` is already `Path(__file__).parent.parent / "data" / "raw" / "bus"`, resolved relative to wherever the file runs — once it's part of `backend`'s own process, this path lands on `backend`'s own volume automatically, with zero code change needed in the collector itself.

If `run_forever()` ever raises past its own internal handling, catch, log via `logging.exception`, and restart immediately (no 24h wait, unlike the nightly loop below — a crashed poller should come back fast, not once a day).

### 2. Ingestion folded into the existing nightly aggregation cycle

`_run_aggregation_sync()` already runs once immediately at startup and then every 24h. Add three calls at the top of it, before the existing `run_aggregate()` fold:
```python
try:
    download_subwaydata.run_backfill(SUBWAY_RAW_DIR, datetime.now(timezone.utc).date())
except Exception:
    logging.exception("subway backfill download failed")
try:
    ingest_subwaydata.run_ingest(SUBWAY_RAW_DIR, conn, _subway_trip_index(), _subway_static_index())
except Exception:
    logging.exception("subway ingest failed")
try:
    derive_bus_arrival_events.run_derive(BUS_RAW_DIR, conn)
except Exception:
    logging.exception("bus derive failed")
run_aggregate(conn)  # existing call, unchanged -- still relies on the outer loop's existing catch
```
Each new step gets its **own** independent `try/except` — a subway download network blip must not also block that cycle's bus derive or aggregation fold, matching the same per-step isolation principle already established in Phase 3's trip monitor (one trip's failure doesn't block the others in the same cycle). `run_aggregate()` itself is untouched, still protected by the existing outer-loop catch exactly as today.

Subway's `TripIndex` and 565K-row static stop-times index are expensive to build — cached once at module level in `main.py` (`_subway_trip_index()`/`_subway_static_index()`, lazy-built-and-cached, matching the existing `_default_route_index()` pattern in `risk_engine.py`), not rebuilt every night.

Both `download_subwaydata.run_backfill`/`ingest_subwaydata.run_ingest`/`derive_bus_arrival_events.run_derive` are called with their existing default arguments every night — their built-in idempotency makes repeated full-window calls cheap (already-done days are skipped via a fast existence/DB check), so no new "first run vs. nightly" argument distinction is needed.

### 3. One-time data migration (controller-driven, not delegated to a subagent)

Stream the bus collector's several weeks of already-accumulated raw ndjson/gz files from its own Railway volume onto `backend`'s volume via `railway ssh`, before the old service is touched — same technique already used for `ferry.zip`/`graph.obj` uploads this session. This preserves real, already-collected historical data regardless of the architecture change.

### 4. Decommission the standalone bus-collector Railway service

Only after the in-process version is confirmed polling and writing correctly on `backend` — running both simultaneously would double-poll MTA's live feed and write to two different places for no benefit. This is a real, hard-to-reverse action, confirmed explicitly as its own step, not silently bundled into the rest of the deploy.

## Error handling

- Each of the 3 new nightly-ingestion steps fails independently and is logged, never blocking its siblings or aborting that cycle's aggregation fold.
- The bus-collector loop crash-and-restart is immediate, not on the 24h cadence.
- `arrival_events`/`reliability_buckets` staying honestly empty for a route/day where source data genuinely doesn't exist is correct, expected behavior (matches `get_risk`'s existing `n<200` → `"insufficient"` philosophy) — this pipeline's job is to stop silently discarding data that *does* exist, not to fabricate anything for data that doesn't.

## Testing

- Extend `backend/tests/test_main.py` (the exact existing pattern already used for `_run_aggregation_loop`'s failure-isolation test: mock `app.main`'s functions directly, use an `asyncio.sleep`-raises-`CancelledError` trick to end an infinite loop after one cycle) — new tests confirming: each of the 3 new ingestion steps is called; a failure in one doesn't prevent the others or the existing aggregation call from still running; the bus-collector loop restarts immediately (not after a 24h sleep) if `run_forever()` raises.
- `derive_bus_arrival_events.py`/`ingest_subwaydata.py`/`download_subwaydata.py` already have full existing test coverage — this work only calls them, doesn't change their logic, so no new tests needed there.
- Live verification (after deploy): confirm `arrival_events` genuinely gains rows after one real nightly cycle (or a manually-triggered one via `railway ssh`, rather than waiting up to 24h), and that `reliability_buckets` gains rows once enough days accumulate.

## Explicitly out of scope

- No changes to `download_subwaydata.py`/`ingest_subwaydata.py`/`derive_bus_arrival_events.py`'s own logic — already correct.
- No change to the nightly aggregation fold logic itself (`run_aggregate`/`aggregate_reliability_buckets.py`).
- Push notifications and the confirmation-gated step-by-step guidance feature (see below) — deliberately sequenced after this work, not part of it.

## Next up (deliberately after this work, not part of it)

Two related features discussed this session, explicitly deferred until this data-ingestion pipeline is live and verified, since real reliability data is the higher-priority gap:

**Confirmation-gated step-by-step guidance.** For an actively-guided trip, instead of handing over the whole route up front, proactively check in at each calculated waypoint arrival time ("You're supposed to arrive at your transfer at [station], are you there?"), wait for confirmation, then give the next step — including the "what to look for" headsign/signage guidance from the walking-navigation design — repeating for every subsequent waypoint, not just the first transfer.

**Real push notification infrastructure**, which the above genuinely needs to be "automatic" rather than "surfaces on your next message" — confirmed with the user this is a real, self-contained infrastructure project (frontend service worker + permission UX, backend push-subscription storage + VAPID key signing + send logic via a library like `pywebpush`), comparable in size to the walking-navigation work, not something foldable into a smaller feature. Explicitly named in this product's own spec as the core differentiator ("proactively reaching someone not looking at their phone") and already flagged as a known, deliberate Phase 3 deferral in `CLAUDE.md` — this is where it actually gets built, once prioritized.
