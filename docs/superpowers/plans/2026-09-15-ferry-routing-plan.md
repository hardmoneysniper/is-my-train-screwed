# Ferry Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OTP can plan a trip that includes a ferry leg, the agent narrates it, and a ferry-involving transfer/deadline degrades honestly to "insufficient"/unavailable — no reliability collector, no real-time ferry integration.

**Architecture:** One new GTFS feed staged into the existing OTP graph-build pipeline; two independent zip-name lists extended so ferry stops/routes resolve everywhere `find_stop`/transfer-risk lookups already work; zero changes to `_fetch_bucket`/`deadline.py` (verified their existing fallthrough already degrades ferry correctly); two tool-description strings updated for accurate narration.

**Tech Stack:** Same as the rest of this project — Python/FastAPI backend, raw sqlite3, OTP 2.7.0 (Docker, graph rebuild required, no Docker access in this session — every graph-affecting task ends with instructions for the user to run locally).

## Global Constraints

- **LLM agents never compute numbers, routes, or probabilities.** No change to this — ferry legs flow through the exact same `plan_route`/`get_risk` narration path every other mode already uses.
- **Never fabricate.** A ferry-involving transfer/deadline must degrade to `quality="insufficient"`/`None`, never a guessed number.
- **Minimalism** (`CLAUDE.md`): no new services, no new collector, no filtering logic for `ferry.zip` (unlike bus, it needs none — see design doc's memory-budget verification).
- **Scope, confirmed with the user:** routing only. Do not build a ferry reliability collector, do not touch `router-config.json` (no real-time ferry updater in scope), do not fix the unrelated `bus_gtfs/` gitignore gap.

Design doc: `docs/superpowers/specs/2026-09-15-ferry-routing-design.md` — read it in full before starting; it has the live-verified facts (route-name collisions checked, OSM bbox compatibility checked, the `RES`/`RWS` `route_type=3` quirk, and the traced proof that `_fetch_bucket`/`deadline.py` need zero code changes) this plan builds against without re-deriving them.

---

## Task 1: Ferry GTFS staged into the OTP build pipeline + repo hygiene fix

**Files:**
- Move: `ferry.zip` (repo root) → `backend/data/gtfs/ferry.zip`
- Modify: `backend/scripts/prepare_otp_data.py`
- Modify: `backend/otp_config/build-config.json`

**Interfaces:**
- Consumes: nothing new.
- Produces: `backend/data/otp/ferry.zip` (staged copy, created when `prepare_otp_data.py` runs), a `NYCFerry` entry in `build-config.json`'s `transitFeeds` that a local `docker compose run --rm otp --build --save` will pick up.

- [ ] **Step 1: Move the file**

```bash
mkdir -p backend/data/gtfs
mv ferry.zip backend/data/gtfs/ferry.zip
```
`backend/data/gtfs/` is already covered by the root `.gitignore`'s `backend/data/` rule (confirmed via `git check-ignore -v backend/data/gtfs/subway.zip` returning a match) — this alone fixes the "untracked and not gitignored" hygiene gap, no `.gitignore` edit needed.

- [ ] **Step 2: Verify the move didn't break anything already tracking the old path**

```bash
git status --short | grep -i ferry
```
Expected: `ferry.zip` no longer appears as untracked at the repo root; `backend/data/gtfs/ferry.zip` does not appear at all (correctly gitignored).

- [ ] **Step 3: Add `ferry.zip` to `prepare_otp_data.py`'s unfiltered-copy list**

In `backend/scripts/prepare_otp_data.py`, `GTFS_FILES` currently reads:
```python
GTFS_FILES = {
    "subway.zip": GTFS_DIR / "subway.zip",
}
```
Change to:
```python
GTFS_FILES = {
    "subway.zip": GTFS_DIR / "subway.zip",
    "ferry.zip": GTFS_DIR / "ferry.zip",
}
```
No filtering needed (unlike `FILTERED_BUS_FILES`) — `ferry.zip` is 55KB, 9 routes, 51 stops; the design doc's memory-budget analysis (verified against real measured RSS on the current graph) confirms this is roughly two orders of magnitude smaller than even one already-filtered bus feed, well inside the ~6GB of headroom already established.

- [ ] **Step 4: Add the ferry feed entry to `build-config.json`**

In `backend/otp_config/build-config.json`, the `transitFeeds` array currently ends with the `MTA_NYCT_Bus_Manhattan` entry. Add a fourth entry:
```json
{
  "osm": [
    {
      "source": "file:///var/opentripplanner/NewYork_trimmed.osm.pbf"
    }
  ],
  "transitFeeds": [
    {
      "type": "gtfs",
      "feedId": "MTA_NYCT_Subway",
      "source": "file:///var/opentripplanner/subway.zip"
    },
    {
      "type": "gtfs",
      "feedId": "MTABC",
      "source": "file:///var/opentripplanner/bus_filtered.zip"
    },
    {
      "type": "gtfs",
      "feedId": "MTA_NYCT_Bus_Manhattan",
      "source": "file:///var/opentripplanner/bus_manhattan_filtered.zip"
    },
    {
      "type": "gtfs",
      "feedId": "NYCFerry",
      "source": "file:///var/opentripplanner/ferry.zip"
    }
  ]
}
```
Validate it's still well-formed JSON: `python -c "import json; json.load(open('backend/otp_config/build-config.json'))"` — expected: no output, no exception.

- [ ] **Step 5: Run `prepare_otp_data.py` locally and confirm `ferry.zip` gets staged**

```bash
cd backend && python scripts/prepare_otp_data.py
```
Expected: prints `[prepare_otp_data] OTP data directory ready at ...`, no `SystemExit`. Then:
```bash
ls backend/data/otp/ferry.zip
```
Expected: file exists, same size as `backend/data/gtfs/ferry.zip` (a straight copy, not filtered).

- [ ] **Step 6: Commit**

```bash
git add backend/data/gtfs/ferry.zip backend/scripts/prepare_otp_data.py backend/otp_config/build-config.json
git commit -m "Stage ferry GTFS into the OTP build pipeline"
```

---

## Task 2: `risk_engine.py` ferry agency mapping + route index

**Files:**
- Modify: `backend/app/risk_engine.py:66,69-77`
- Test: `backend/tests/test_risk_engine.py` (extend)

**Interfaces:**
- Consumes: nothing new from other tasks.
- Produces: `_MODE_TO_AGENCY["FERRY"] = "ferry"`, `"ferry.zip"` present in `_GTFS_ZIP_NAMES` — later tasks (none in this plan, but the design doc's "no other changes needed" claim) depend on this being in place for `_fetch_bucket`'s existing fallthrough to actually receive `agency="ferry"` rather than `None`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_risk_engine.py` (reuses this file's existing `conn`/`route_index` fixtures and `_local_ms` helper — read the top of the file first if you haven't already):
```python
def test_ferry_transfer_degrades_to_insufficient_not_crash(conn, tmp_path):
    ferry_zip = _routes_zip(tmp_path, "ferry.zip", [("AS", "AS")])
    subway_zip = _routes_zip(tmp_path, "subway.zip", [("F", "F")])
    route_index_with_ferry = RouteIndex.from_gtfs([subway_zip, ferry_zip])

    itinerary = Itinerary(
        duration_seconds=1800,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="FERRY",
                route_short_name="AS",
                from_stop_id="NYCFerry:4",
                from_stop_name="Hunters Point South",
                to_stop_id="NYCFerry:17",
                to_stop_name="East 34th Street",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 30, 0),
            ),
        ],
    )
    # No reliability_buckets rows for agency='ferry' exist -- none ever
    # will, in this scope (no ferry collector). This must degrade
    # honestly, not crash and not fabricate a number.
    results = get_risk(itinerary, conn=conn, route_index=route_index_with_ferry)

    assert len(results) == 1
    assert results[0].quality == "insufficient"
    assert results[0].p_miss is None
```
This will fail before Step 3 not because `get_risk` crashes (it already wouldn't — `_MODE_TO_AGENCY.get("FERRY")` returns `None` today, which `_transfer_risk_for_pair` already handles as `quality="insufficient"`), but because `route_index_with_ferry.resolve("AS")` returns `None` (the route isn't in `_GTFS_ZIP_NAMES` yet in production code, though this test builds its own local `RouteIndex` so it's really only proving the *test itself* is well-formed at this stage). The real assertion this step protects is Step 3's `_MODE_TO_AGENCY` addition — run it now to confirm today's behavior really is `quality="insufficient"` already (it should pass even before Step 3, since `_MODE_TO_AGENCY.get("FERRY")` already safely returns `None` via `.get()`) and that Step 3 doesn't change that outcome, only *why* it happens (proves the case explicitly rather than leaving it implicit).

- [ ] **Step 2: Run test to verify it passes even before the code change (confirms the safe-by-default baseline)**

Run: `cd backend && python -m pytest tests/test_risk_engine.py::test_ferry_transfer_degrades_to_insufficient_not_crash -v`
Expected: PASS — `.get("FERRY")` on a dict without that key already returns `None` safely; this is the "no crash" half of the design doc's claim, true before any code change.

- [ ] **Step 3: Add the `FERRY` entry and comment**

In `backend/app/risk_engine.py`, change:
```python
_MODE_TO_AGENCY = {"SUBWAY": "subway", "BUS": "bus"}
```
to:
```python
_MODE_TO_AGENCY = {
    "SUBWAY": "subway",
    "BUS": "bus",
    "FERRY": "ferry",
    # 2 of NYC Ferry's 9 real GTFS routes (RES "Rockaway East", RWS
    # "Rockaway West") are route_type=3 (bus) in the source feed, not
    # route_type=4 (ferry) -- a real shuttle-bus segment bundled into the
    # ferry feed, not a data error. OTP tags those legs mode="BUS", so
    # they resolve via the "BUS" entry above, not this one -- correct
    # given the source data, not a gap to special-case.
}
```
And extend `_GTFS_ZIP_NAMES`:
```python
_GTFS_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
    "ferry.zip",
]
```
(This reads from `backend/data/gtfs/ferry.zip`, staged by Task 1 — verify that file exists before running the next step, or `RouteIndex.from_gtfs` will raise on a missing file the moment any code path touches the real, non-test `_default_route_index()`.)

- [ ] **Step 4: Run the full test file**

Run: `cd backend && python -m pytest tests/test_risk_engine.py -v`
Expected: all pass, including the new test, with no regressions in the existing suite.

- [ ] **Step 5: Commit**

```bash
git add backend/app/risk_engine.py backend/tests/test_risk_engine.py
git commit -m "Add ferry to risk_engine's agency mapping and route index"
```

---

## Task 3: `nearest_stop.py` ferry zip list

**Files:**
- Modify: `backend/app/routing/nearest_stop.py:90-98`
- Test: `backend/tests/test_nearest_stop.py` (extend)

**Interfaces:**
- Consumes: `backend/data/gtfs/ferry.zip` (staged by Task 1).
- Produces: `get_stop_index()`'s cached singleton includes ferry stops once this is deployed — no other task in this plan depends on this directly, but it's required for `find_stop` to ever resolve a ferry stop by name.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_nearest_stop.py`:
```python
def test_ferry_zip_is_in_the_loaded_gtfs_list():
    from app.routing.nearest_stop import _GTFS_ZIP_NAMES
    assert "ferry.zip" in _GTFS_ZIP_NAMES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_nearest_stop.py::test_ferry_zip_is_in_the_loaded_gtfs_list -v`
Expected: FAIL — `ferry.zip` not yet in the list.

- [ ] **Step 3: Add `ferry.zip` to the list**

In `backend/app/routing/nearest_stop.py`, `_GTFS_ZIP_NAMES` currently reads (7 entries, `subway.zip` through `bus_staten_island.zip`). Add an 8th entry:
```python
_GTFS_ZIP_NAMES = [
    "subway.zip",
    "bus.zip",
    "bus_manhattan.zip",
    "bus_bronx.zip",
    "bus_brooklyn.zip",
    "bus_queens.zip",
    "bus_staten_island.zip",
    "ferry.zip",
]
```
This is a **separate, independently-defined list from `risk_engine.py`'s** (confirmed during design — not shared code) — Task 2's edit does not cover this one.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_nearest_stop.py -v`
Expected: all pass, including the new test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routing/nearest_stop.py backend/tests/test_nearest_stop.py
git commit -m "Add ferry to nearest_stop's loaded GTFS list"
```

---

## Task 4: `deadline.py` regression test for the ferry all-or-nothing consequence

**Files:**
- Test: `backend/tests/test_deadline.py` (extend — no production code change in this task, verifying an existing-behavior claim from the design doc)

**Interfaces:**
- Consumes: `Leg(mode="FERRY", ...)`, `_MODE_TO_AGENCY["FERRY"]` (Task 2).
- Produces: nothing new — this task exists to prove, not build.

- [ ] **Step 1: Write the test**

Add to `backend/tests/test_deadline.py` (reuses this file's `conn`/`_local_ms`/`_insert_bucket` fixtures — read the top of the file first):
```python
def _ferry_leg(route_short_name, from_id, to_id, from_name, to_name, start, end):
    return Leg(
        mode="FERRY",
        route_short_name=route_short_name,
        from_stop_id=f"NYCFerry:{from_id}",
        from_stop_name=from_name,
        to_stop_id=f"NYCFerry:{to_id}",
        to_stop_name=to_name,
        start_time_ms=start,
        end_time_ms=end,
    )


def test_ferry_leg_makes_whole_itinerary_deadline_unavailable(conn, tmp_path):
    ferry_zip = _routes_zip(tmp_path, "ferry.zip", [("AS", "AS")])
    subway_zip = _routes_zip(tmp_path, "subway.zip", [("F", "F")])
    route_index_with_ferry = RouteIndex.from_gtfs([subway_zip, ferry_zip])

    # Sufficient data for the subway leg -- if ferry weren't blocking the
    # whole estimate, this alone would produce a real number.
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": _WORKED_EXAMPLE_COUNTS}),
        n_observations=250,
    )

    leg1_end = _local_ms(2026, 8, 24, 8, 10, 0)
    leg2_start = _local_ms(2026, 8, 24, 8, 15, 0)
    leg2_end = _local_ms(2026, 8, 24, 8, 30, 0)

    itinerary = Itinerary(
        duration_seconds=1800,
        legs=[
            _subway_leg("F", "B06N", "127N", "Roosevelt Island", "Lexington Av/63 St", _local_ms(2026, 8, 24, 8, 0, 0), leg1_end),
            _ferry_leg("AS", "4", "17", "Hunters Point South", "East 34th Street", leg2_start, leg2_end),
        ],
    )

    deadline_ts = leg2_end + 7200_000
    result = compute_deadline_threshold(itinerary, deadline_ts, conn=conn, route_index=route_index_with_ferry)

    # No reliability_buckets rows for agency='ferry' exist and never will
    # in this scope -- the whole itinerary's estimate must come back
    # unavailable, not a partial number computed from the subway leg
    # alone. This is the existing all-or-nothing behavior (Task 4,
    # Phase 2), not new logic -- this test proves it, doesn't change it.
    assert result is None
```

- [ ] **Step 2: Run test to verify it passes without any production code change**

Run: `cd backend && python -m pytest tests/test_deadline.py::test_ferry_leg_makes_whole_itinerary_deadline_unavailable -v`
Expected: PASS immediately — this is the whole point of the test (proving the design doc's "no code change needed" claim is actually true, not just reasoned about). If this fails, the design doc's claim was wrong and `deadline.py` needs a real fix before this plan can be considered complete — escalate rather than silently patching around it.

- [ ] **Step 3: Run the full test file to confirm no regressions**

Run: `cd backend && python -m pytest tests/test_deadline.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_deadline.py
git commit -m "Add regression test: ferry leg makes deadline estimate unavailable"
```

---

## Task 5: Tool description narration accuracy

**Files:**
- Modify: `backend/app/agents/tools.py:4,19`
- Test: `backend/tests/test_conversation_agent.py` (extend, minimal)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing new consumed by other tasks — purely narration-accuracy text.

**Note:** `conversation_agent.py`'s `SYSTEM_PROMPT` was checked directly (grep for "subway"/"bus" literal text) and contains **no mode-specific language at all** — it already reads generically ("You are a NYC transit trip advisor"), so it needs no edit. Only `tools.py`'s two tool-description strings explicitly say "subway/bus."

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_conversation_agent.py`:
```python
def test_tool_descriptions_mention_ferry():
    from app.agents.tools import FIND_STOP_TOOL, PLAN_ROUTE_TOOL
    assert "ferry" in PLAN_ROUTE_TOOL["description"].lower()
    assert "ferry" in FIND_STOP_TOOL["description"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_conversation_agent.py::test_tool_descriptions_mention_ferry -v`
Expected: FAIL — neither string mentions ferry yet.

- [ ] **Step 3: Update the two description strings**

In `backend/app/agents/tools.py`, change:
```python
PLAN_ROUTE_TOOL = {
    "name": "plan_route",
    "description": "Get a subway/bus itinerary between two lat/lon points via OpenTripPlanner. Never estimate a route yourself — always call this.",
```
to:
```python
PLAN_ROUTE_TOOL = {
    "name": "plan_route",
    "description": "Get a subway/bus/ferry itinerary between two lat/lon points via OpenTripPlanner. Never estimate a route yourself — always call this.",
```
And change:
```python
FIND_STOP_TOOL = {
    "name": "find_stop",
    "description": "Look up a subway or bus stop by name or partial name (e.g. 'Roosevelt Island', '86 St') to get its coordinates. Call this when the user names a place instead of giving exact coordinates, then use the returned stop's lat/lon with plan_route. Never guess coordinates yourself.",
```
to:
```python
FIND_STOP_TOOL = {
    "name": "find_stop",
    "description": "Look up a subway, bus, or ferry stop by name or partial name (e.g. 'Roosevelt Island', '86 St') to get its coordinates. Call this when the user names a place instead of giving exact coordinates, then use the returned stop's lat/lon with plan_route. Never guess coordinates yourself.",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_conversation_agent.py -v`
Expected: all pass, including the new test, no regressions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/tools.py backend/tests/test_conversation_agent.py
git commit -m "Update tool descriptions to mention ferry"
```

---

## Task 6: Local graph rebuild + deploy + live verification

**Files:** none new — this task ships Tasks 1-5's code/config, plus a real rebuilt `graph.obj`, to the already-live `otp` Railway service.

**What "done" means for this task:** matching this project's established precedent (every prior OTP-graph change) — a real, live smoke test is the actual completion gate, not green CI. This task requires the user's local Docker (not available in an agentic session) for the rebuild step; the deploy/verify steps are done via the Railway API + a real `/chat` call, same tooling already used throughout this project's history.

1. **User runs the local graph rebuild** (from `backend/`, in their own terminal): `docker compose run --rm otp --build --save`. Confirm it completes without an OOM kill (expected — see design doc's memory-budget analysis; if it does OOM despite that analysis, stop and re-open the design doc's assumptions rather than guessing a fix).
2. **Upload the new `graph.obj` to the `otp` Railway service's volume** via `railway ssh` (same pattern as every prior graph upload in this project's history — temporarily override `startCommand` to `sleep infinity` if the container is mid-restart-loop, upload, restore the real `startCommand`).
3. **Re-upload `router-config.json` to the same volume** — required every time `graph.obj` is re-uploaded, per the already-documented Docker-volume-shadowing gotcha (a newly-built graph embeds whatever router config was present at *build* time; skipping this re-upload silently reverts to a stale local-dev config).
4. **Redeploy the `otp` service** and confirm `status: SUCCESS` via the Railway API — and confirm the deployed commit hash matches what's expected, not just trusting `SUCCESS` (this project's own established discipline: a stale-branch fallback has silently passed as `SUCCESS` before).
5. **Real live call**: `POST /chat` planning a trip between two real NYC Ferry stops (or an origin/destination pair where ferry is plausibly part of the best route — e.g. Hunters Point South to East 34th Street, both real stops on the `AS` route confirmed in this session's GTFS inspection). Confirm the response narrates a ferry leg with real route/stop names, not a fabricated one and not an error.
6. **Real live call**: a trip that also crosses a transfer (e.g. subway to ferry) — confirm `get_risk` returns `quality="insufficient"` for that transfer (not a crash, not a fabricated percentage) via the actual chat response text.
7. Document the real observed results (exact request/response, exact deployment commit hash, exact ferry stop names used) in `CLAUDE.md`'s deployment notes, matching the existing documentation density for every prior deploy in this project.

---

## Execution

Tasks 1-5 are each small, independently testable, TDD-shaped units suitable for subagent-driven-development. Task 6 cannot be delegated to a fresh subagent — it requires live coordination with the user's own local Docker session (which no agent in this session can drive) interleaved with Railway API calls; it should be executed directly by whoever is driving this plan, the same way every prior OTP-graph-affecting deploy in this project was done.

Order: 1 must come first (stages the file Tasks 2/3 read). 2, 3, 4, 5 are independent of each other and can run in any order. 6 must come last, after 1-5 are merged.
