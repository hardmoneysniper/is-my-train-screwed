# Reliability Data Ingestion Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real reliability data actually reaches `arrival_events`/`reliability_buckets` in production — currently both are completely empty despite weeks of real bus data already collected and subwaydata.nyc's live-downloadable backfill, because the scripts that would populate them were never wired into any recurring production process.

**Architecture:** Fold the standalone bus collector into `backend`'s own in-process lifecycle (same Railway cross-volume constraint that already forced the nightly aggregator in-process in Phase 2); extend the existing nightly aggregation cycle to run subway backfill/ingest and bus derive first, each independently isolated so one failing doesn't block the others.

**Tech Stack:** Same as the rest of this project — Python/FastAPI backend, raw sqlite3, `asyncio`/`asyncio.to_thread` for background loops (existing pattern, not new).

## Global Constraints

- **Never fabricate.** `arrival_events`/`reliability_buckets` staying honestly empty for a route/day with no real source data is correct — this pipeline stops discarding data that *does* exist, it does not manufacture anything for data that doesn't.
- **Minimalism** (`CLAUDE.md`): no changes to `download_subwaydata.py`/`ingest_subwaydata.py`/`derive_bus_arrival_events.py`'s own logic (already correct, already tested) or to `run_aggregate`/`aggregate_reliability_buckets.py` — this plan only wires already-correct pieces into an already-existing in-process loop pattern.
- **Per-step failure isolation**: each new step (bus-collector restart, subway backfill, subway ingest, bus derive) is independently caught and logged — one failing must never block its siblings or the existing aggregation fold, matching the established per-trip-isolation principle from Phase 3's trip monitor.

Design doc: `docs/superpowers/specs/2026-09-21-data-ingestion-pipeline-design.md` — read it in full before starting; it has the live-verified facts (empty tables confirmed directly, all three scripts already idempotent, exact cross-volume constraint) this plan builds against without re-deriving them.

---

## Task 1 — Fold the bus collector into `backend`'s in-process lifecycle

**Files:**
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: `backend.collectors.bus_collector.run_forever` (existing, unchanged — a synchronous, already-self-healing `while True` poller that only ever raises past its own internal handling if `_api_key()` fails at startup, i.e. `MTA_BUSTIME_API_KEY` is unset).
- Produces: a new `_run_bus_collector_loop()` async function wired into `lifespan`, following the exact same shape as the existing `_run_aggregation_loop`/`_run_monitor_loop`.

**Key decisions (from the design doc — implement exactly):** `run_forever()` already retries internally on every transient failure (network errors, unexpected exceptions) via its own backoff loop — it essentially never raises under normal operation. The wrapper here is a safety net for the one thing that *can* raise past it (a missing `MTA_BUSTIME_API_KEY`, checked once before its `while True` even starts). If `run_forever()` does raise, catch, log, and retry **immediately** — no 24h sleep like the aggregation loop; a crashed poller should come back fast.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_main.py` (reuses this file's exact existing pattern — read `test_aggregation_loop_survives_a_failed_run_and_keeps_going` above it first):
```python
async def test_bus_collector_loop_restarts_immediately_after_a_failure():
    call_count = {"n": 0}

    def failing_run_forever():
        call_count["n"] += 1
        raise RuntimeError("boom")

    sleep_calls = []

    async def record_sleep_then_stop(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    with patch("app.main.run_forever", side_effect=failing_run_forever), \
         patch("app.main.asyncio.sleep", side_effect=record_sleep_then_stop), \
         patch("app.main.logging.exception") as mock_log_exception:
        with pytest.raises(asyncio.CancelledError):
            await _run_bus_collector_loop()

    assert call_count["n"] == 1
    mock_log_exception.assert_called_once()
    # Restarts immediately on failure, not on the 24h aggregation cadence --
    # confirm whatever sleep duration is used is small (seconds, not a day).
    assert sleep_calls == [] or sleep_calls[0] < 3600
```
Import `_run_bus_collector_loop` alongside the existing `_run_aggregation_loop` import at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_main.py::test_bus_collector_loop_restarts_immediately_after_a_failure -v`
Expected: FAIL — `_run_bus_collector_loop` doesn't exist yet.

- [ ] **Step 3: Add the loop and wire it into `lifespan`**

In `backend/app/main.py`, add the import:
```python
from collectors.bus_collector import run_forever
```
Add, near the other loop functions (after `_run_monitor_loop`):
```python
# Data-ingestion pipeline: the bus collector previously ran as its own
# standalone Railway service with its own volume -- but derive_bus_
# arrival_events.py needs its raw output on THIS service's volume, and
# Railway doesn't support sharing a volume across services (the same
# constraint that already forced the nightly aggregator in-process,
# above). Folding the collector in here means its own DATA_DIR (already
# resolved relative to wherever it runs) lands on this service's volume
# automatically, with zero change to the collector's own logic.
BUS_COLLECTOR_RESTART_DELAY_S = 5


async def _run_bus_collector_loop():
    """run_forever() already retries internally on every transient
    failure (network errors, unexpected exceptions -- see its own
    backoff loop) and essentially never raises under normal operation.
    This wrapper exists for the one thing that CAN raise past it: a
    missing MTA_BUSTIME_API_KEY, checked once before its while True even
    starts. Restarts immediately (a short fixed delay, not the 24h
    aggregation cadence) since a crashed poller should come back fast."""
    while True:
        try:
            # Blocking, long-running I/O (its own internal time.sleep
            # polling loop) -- hand it to a worker thread so it never
            # stalls /chat or /trip/plan requests being served
            # concurrently, same reasoning as _run_aggregation_sync.
            await asyncio.to_thread(run_forever)
        except Exception:
            logging.exception("bus collector loop failed")
        await asyncio.sleep(BUS_COLLECTOR_RESTART_DELAY_S)
```
In `lifespan`, add alongside the existing two tasks:
```python
task = asyncio.create_task(_run_aggregation_loop())
monitor_task = asyncio.create_task(_run_monitor_loop())
bus_collector_task = asyncio.create_task(_run_bus_collector_loop())
```
and cancel/await it the same way on shutdown:
```python
task.cancel()
monitor_task.cancel()
bus_collector_task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass
try:
    await monitor_task
except asyncio.CancelledError:
    pass
try:
    await bus_collector_task
except asyncio.CancelledError:
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: all pass, including the new test, no regressions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_main.py
git commit -m "Fold bus collector into backend's in-process lifecycle"
```

---

## Task 2 — Wire nightly subway backfill/ingest and bus derive into the existing aggregation cycle

**Files:**
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: `scripts.download_subwaydata.run_backfill`, `scripts.ingest_subwaydata.run_ingest` + its `RAW_DIR` constant, `scripts.ingest_subwaydata`'s dependencies (`app.realtime_proxy.SUBWAY_ZIP`/`TripIndex`, `ingest_subwaydata.build_static_stop_times_index`), `scripts.derive_bus_arrival_events.run_derive` + its `RAW_DIR` constant — all existing, unchanged, already fully idempotent and already fully tested in isolation.
- Produces: `_run_aggregation_sync()` extended with three new, independently-isolated steps before its existing `run_aggregate()` call.

**Key decisions (from the design doc — implement exactly):** Each of the 3 new steps gets its own `try/except` + `logging.exception` — a failure in one must not prevent the others or the existing `run_aggregate()` call from still running in the same cycle. Reuse the existing `RAW_DIR` constants already defined in `ingest_subwaydata.py` (subway) and `derive_bus_arrival_events.py` (bus) via import — do not redefine new path constants in `main.py`, both already resolve to the correct `backend/data/raw/{subway,bus}` directories. Subway's `TripIndex` and 565K-row static stop-times index are real, non-trivial parsing work — build them once, lazily, cached at module level (matching `risk_engine.py`'s existing `_default_route_index()` pattern), not rebuilt every night.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_main.py`:
```python
async def test_nightly_ingestion_steps_are_independently_isolated():
    fake_conn = MagicMock()
    calls = []

    def failing_backfill(*a, **k):
        calls.append("backfill")
        raise RuntimeError("subway backfill boom")

    def ok_ingest(*a, **k):
        calls.append("ingest")

    def failing_derive(*a, **k):
        calls.append("derive")
        raise RuntimeError("bus derive boom")

    def ok_aggregate(conn):
        calls.append("aggregate")

    with patch("app.main.get_connection", return_value=fake_conn), \
         patch("app.main.download_subwaydata_run_backfill", side_effect=failing_backfill), \
         patch("app.main.ingest_subwaydata_run_ingest", side_effect=ok_ingest), \
         patch("app.main.derive_bus_arrival_events_run_derive", side_effect=failing_derive), \
         patch("app.main.run_aggregate", side_effect=ok_aggregate), \
         patch("app.main.logging.exception") as mock_log_exception:
        _run_aggregation_sync()

    # All four steps were attempted despite two of them failing -- neither
    # failure blocked its siblings or the final aggregate call.
    assert calls == ["backfill", "ingest", "derive", "aggregate"]
    assert mock_log_exception.call_count == 2
    fake_conn.close.assert_called_once()
```
Import `_run_aggregation_sync` alongside the existing imports at the top of the file (it's already used by the existing aggregation-loop test indirectly, but not currently imported directly — check and add if missing).

Note the patch targets: `main.py`'s new code should import each function under a distinct local name (`download_subwaydata_run_backfill`, `ingest_subwaydata_run_ingest`, `derive_bus_arrival_events_run_derive` — see Step 3) specifically so each can be mocked independently in tests without colliding with any other `run_backfill`/`run_ingest`/`run_derive`-named import elsewhere.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_main.py::test_nightly_ingestion_steps_are_independently_isolated -v`
Expected: FAIL — the three new steps don't exist yet in `_run_aggregation_sync`.

- [ ] **Step 3: Extend `_run_aggregation_sync`**

In `backend/app/main.py`, add these imports:
```python
from datetime import datetime, timezone
from scripts.download_subwaydata import run_backfill as download_subwaydata_run_backfill
from scripts.ingest_subwaydata import (
    RAW_DIR as SUBWAY_RAW_DIR,
    run_ingest as ingest_subwaydata_run_ingest,
    build_static_stop_times_index,
)
from scripts.derive_bus_arrival_events import (
    RAW_DIR as BUS_RAW_DIR,
    run_derive as derive_bus_arrival_events_run_derive,
)
from app.realtime_proxy import SUBWAY_ZIP, TripIndex
```
Add module-level caching for the subway indices, near the other module-level state:
```python
_subway_trip_index_cache: TripIndex | None = None
_subway_static_index_cache: dict | None = None


def _subway_trip_index() -> TripIndex:
    global _subway_trip_index_cache
    if _subway_trip_index_cache is None:
        _subway_trip_index_cache = TripIndex(SUBWAY_ZIP)
    return _subway_trip_index_cache


def _subway_static_index() -> dict:
    global _subway_static_index_cache
    if _subway_static_index_cache is None:
        _subway_static_index_cache = build_static_stop_times_index(SUBWAY_ZIP)
    return _subway_static_index_cache
```
Change `_run_aggregation_sync` from:
```python
def _run_aggregation_sync():
    conn = get_connection()
    try:
        run_aggregate(conn)
    finally:
        conn.close()
```
to:
```python
def _run_aggregation_sync():
    conn = get_connection()
    try:
        try:
            download_subwaydata_run_backfill(SUBWAY_RAW_DIR, datetime.now(timezone.utc).date())
        except Exception:
            logging.exception("subway backfill download failed")
        try:
            ingest_subwaydata_run_ingest(SUBWAY_RAW_DIR, conn, _subway_trip_index(), _subway_static_index())
        except Exception:
            logging.exception("subway ingest failed")
        try:
            derive_bus_arrival_events_run_derive(BUS_RAW_DIR, conn)
        except Exception:
            logging.exception("bus derive failed")
        run_aggregate(conn)
    finally:
        conn.close()
```
Update `_run_aggregation_loop`'s docstring to mention it now also runs subway backfill/ingest and bus derive first, not just the reliability_buckets fold (a one-line docstring edit, not a behavior change — the outer loop's own try/except/sleep structure is otherwise untouched).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: all pass, including both new tests, no regressions.

- [ ] **Step 5: Run the full backend test suite**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: all pass (should be 189 pre-existing + 2 new = 191).

- [ ] **Step 6: Commit**

```bash
git add backend/app/main.py backend/tests/test_main.py
git commit -m "Wire nightly subway backfill/ingest and bus derive into the aggregation cycle"
```

---

## Task 3 — Migrate historical bus data, deploy, decommission the standalone collector, live-verify

**Files:** none new — this task ships Tasks 1-2's code and migrates real production data. Controller-driven, not delegatable to a subagent (needs live Railway coordination across two services).

**What "done" means for this task:** matching this project's established precedent — a real, live smoke test is the actual completion gate, not green CI.

1. **Add `MTA_BUSTIME_API_KEY` to the `backend` Railway service's environment variables** (it's currently only set on the standalone collector service) — without this, the newly-folded-in collector loop will crash-loop immediately on `_api_key()`'s check.
2. **Migrate historical data**: stream the standalone bus-collector service's accumulated `raw/bus/*.ndjson[.gz]` files onto `backend`'s volume at `backend/data/raw/bus/` via `railway ssh` (same technique already used this session for `ferry.zip`/`graph.obj` — verify byte sizes match after transfer, before deleting anything from the source).
3. **Deploy `backend`** with Tasks 1-2's code. Verify `meta.commitHash` matches the expected commit — don't trust `status: SUCCESS` alone.
4. **Real live verification, in order:**
   - Confirm the in-process bus collector is actually polling: check `backend`'s deployment logs for real `[bus_collector] ... wrote N records` lines appearing on the expected ~60s cadence.
   - Confirm `backend/data/raw/bus/{today}.ndjson` is growing on `backend`'s own volume now (via `railway ssh` + `ls -la`), not just the migrated historical files.
   - Manually trigger one ingestion cycle early rather than waiting up to 24h (e.g., via `railway ssh` running a small Python snippet that calls `_run_aggregation_sync` directly, or by restarting the service to get the immediate-on-startup run) and confirm `arrival_events` genuinely gains real rows (`SELECT COUNT(*) FROM arrival_events` before and after, both agencies).
   - Confirm a real `/chat` query for a previously-`"insufficient"` transfer (e.g., the Roosevelt Island F→Q pair used throughout this project's history) either now returns real `quality="ok"` data or an honestly-still-`"insufficient"` result consistent with how much data has actually accumulated so far — document whichever is genuinely true, don't assume.
5. **Only after the above is confirmed working**: pause or delete the standalone bus-collector Railway service (a real, hard-to-reverse action — confirm with the user before executing, per this project's own established practice for actions of this kind).
6. Document the real numbers/log lines observed in `CLAUDE.md`'s deployment section, matching the existing documentation density for every prior deploy in this project.

---

## Execution

Tasks 1 and 2 are both small, independently testable, TDD-shaped changes to the same file (`main.py`) but functionally independent of each other (a reviewer could approve one while rejecting the other) — suitable for subagent-driven-development, dispatched in either order. Task 3 cannot be delegated — it requires live coordination across two Railway services and a real, hard-to-reverse decommission step; it must run last, after both code tasks are merged.
