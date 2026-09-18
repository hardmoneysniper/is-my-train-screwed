# Ferry Routing — Design

Spec: `is-my-train-screwed-spec.md` (no existing section covers ferry — confirmed via repo-wide case-insensitive grep, zero prior mentions anywhere in code, docs, or spec. The spec's closest analog, line 217, frames adding a wholly new transit mode as an explicit escalation-worthy expansion, not something to do silently — this doc is that explicit treatment).

## Position in the project

Not one of the original 5 phases, and independent of the walking-navigation/location-input design (`2026-09-10-walking-navigation-design.md`) — that doc extends *how* existing modes are narrated and *where* trips start; this doc extends *which modes exist at all*. Separate concern, separate spec.

## Scope: routing only, explicitly not a reliability collector

Confirmed with the user: this adds ferry as a plannable transit mode (OTP can route a trip that includes a ferry leg, the agent narrates it) — it does **not** build a ferry reliability collector. That's a materially bigger, separate effort (comparable to how bus reliability collection was its own multi-week, separately-prioritized project before Phase 2's risk engine could use it, per `CLAUDE.md`'s build-order history) and is out of scope here. Ferry transfers/deadlines correctly and honestly degrade to "insufficient"/unavailable rather than a fabricated number — see below.

## Real data verified this session (not assumed)

- **`ferry.zip`** (supplied by the user, currently sitting untracked at the repo root): real, valid NYC Ferry static GTFS. `agency.txt` confirms `NYC Ferry`. 9 routes (`AS`, `ER`, `GI`, `RES`, `RR`, `RS`, `RWS`, `SB`, `SG`), 51 stops, `feed_info.txt` shows a valid window (2026-07-06 to 2027-12-31). File size 55KB.
- **Route-name collision check**: none of the 9 ferry `route_short_name`s collide with any existing subway or bus `route_short_name` across all 7 currently-loaded GTFS zips — `RouteIndex.resolve()` will work unambiguously once `ferry.zip` is added to the zip lists.
- **OSM extract compatibility**: all 51 ferry stop lat/lons fall inside the already-trimmed OSM bbox `(-74.08, 40.49, -73.70, 40.85)`, including the St. George route's Staten Island terminal (its coordinates sit just inside the bbox edge, despite the trim nominally excluding most of Staten Island). No OSM re-trim needed.
- **A real GTFS-type quirk**: 7 of the 9 routes are `route_type=4` (ferry) in `routes.txt`, but `RES` ("Rockaway East") and `RWS` ("Rockaway West") are `route_type=3` (bus) — likely a shuttle-bus portion bundled into the same feed. OTP will tag their legs `mode="BUS"`, not `"FERRY"`. Documented as a deliberate, understood consequence (see below), not a bug to route around.
- **Memory-budget precedent**: the existing graph (subway + 2 filtered bus feeds) runs with real measured RSS ~2GB against an 8GB Railway plan cap — roughly 6GB of headroom. Ferry's 55KB/9-route/51-stop feed is roughly two orders of magnitude smaller than even one already-trimmed bus feed; the original OOM was driven by route/trip/stop_time *volume*, not feed *count*. No filtering needed for ferry.
- **NYC Ferry does publish live, keyless GTFS-RT** (trip updates, service alerts, and an undocumented-but-working vehicle-positions feed — confirmed by fetching each directly) — **not used in this design**, since the scope is routing-only, but recorded here so a future reliability-collector effort doesn't have to re-derive it. One live gotcha worth keeping on record for that future work: NYC Ferry's `servicealert` path (with "service") returns HTTP 200 with an HTML 404 *page*, not a real 404 status — the correct path is `alert` (confirmed against `ferry.nyc/developer-tools/`'s own documented URL).

## Architecture

### 1. OTP graph inclusion

`backend/otp_config/build-config.json`'s `transitFeeds` list gains one entry:
```json
{ "type": "gtfs", "feedId": "NYCFerry", "source": "file:///var/opentripplanner/ferry.zip" }
```
`backend/scripts/prepare_otp_data.py` gains a step copying `ferry.zip` into `backend/data/otp/ferry.zip` unfiltered (no route-trimming needed, unlike bus — see the memory-budget note above). This is a graph-affecting change: it requires a full local rebuild (`docker compose run --rm otp --build --save`, run by the user directly — this session has no Docker access, same constraint as every prior OTP change) and a redeploy to the `otp` Railway service. The redeploy must repeat the already-documented `router-config.json` re-upload (a Docker volume mounted at the same path a `Dockerfile` `COPY` writes to silently shadows the image's file — re-uploading `graph.obj` without also re-uploading `router-config.json` lets the newly-built graph's embedded local-dev config silently take over again).

### 2. Backend mode/agency handling

Two independent, currently-duplicated `_GTFS_ZIP_NAMES` lists both need `ferry.zip` added, or ferry stops/routes stay invisible to half the codebase while working in the other half:
- `backend/app/risk_engine.py` (feeds `RouteIndex`, used for transfer-risk route resolution)
- `backend/app/routing/nearest_stop.py` (feeds `StopIndex`, used for `find_stop`)

`backend/app/risk_engine.py`'s `_MODE_TO_AGENCY` gains `"FERRY": "ferry"`. A code comment at this dict documents the `RES`/`RWS` quirk: those two routes will arrive tagged `mode="BUS"` by OTP (real `route_type=3` in the source GTFS), so they mechanically resolve via the existing `"BUS"` mapping — correct behavior given the source data, not a gap to special-case.

**No other code changes are needed** — verified by tracing the actual logic, not assumed:
- `_fetch_bucket(conn, agency, ...)` branches on `agency == "subway"` / `agency == "bus"`, falling through to `return None` for anything else. `agency="ferry"` already hits that fallthrough today, before any change — and always will, since no ferry collector exists to ever populate a `reliability_buckets` row with `agency='ferry'` in this scope.
- `_incoming_stat_type()` sends ferry through its `else` branch (`"prediction_error"`) — irrelevant in practice, since `_fetch_bucket` already returns `None` for `agency="ferry"` regardless of which `stat_type` string is requested.
- `deadline.py` reuses both of the above unmodified. A ferry leg in a deadline-mode itinerary correctly makes `compute_deadline_threshold` return `None` for the *whole* itinerary — consistent with Task 4's existing all-or-nothing design philosophy (silently dropping one leg's uncertainty would understate risk, the wrong failure direction for a deadline feature), not a new inconsistency introduced by ferry.

Net effect: a ferry-involving transfer or itinerary degrades exactly the same honest way an under-`n=200` bus corridor already does — `quality="insufficient"` / no deadline estimate — with zero new branching logic required.

### 3. Narration

`backend/app/agents/tools.py`'s `PLAN_ROUTE_TOOL`/`FIND_STOP_TOOL` description strings currently say "subway/bus" — updated to "subway/bus/ferry" so the LLM's own framing doesn't imply ferry doesn't exist. `SYSTEM_PROMPT`'s opening line gets the same update. No other `SYSTEM_PROMPT` change needed — the existing `quality="insufficient"` narration instruction ("say reliability data isn't available yet for that transfer") is already agency-agnostic and needs no ferry-specific carve-out.

### 4. Hygiene fix

`ferry.zip` is currently untracked at the repo root and **not** matched by any `.gitignore` rule (confirmed via `git check-ignore`) — a real risk of accidentally committing a 55KB binary GTFS zip on a future broad `git add`. Moved into `backend/data/gtfs/ferry.zip`, matching where every other raw static GTFS source lives, which *is* covered by the existing `backend/data/` gitignore rule.

(A pre-existing, unrelated `bus_gtfs/` directory at the repo root has the same untracked-and-ungitignored problem — noted here for awareness since it was surfaced during this session's exploration, but left untouched: out of scope for this task, not something the user asked to fix.)

## Error handling

- A ferry leg with no matching reliability data: honest `quality="insufficient"`, never a fabricated number — this is the existing, unmodified degradation path, not new logic.
- A ferry leg inside a deadline-mode itinerary: honest `None` (no estimate), for the same existing reason.
- `RES`/`RWS`'s `mode="BUS"` tagging: handled correctly by the existing bus branch, not a crash risk or a silent misclassification once the comment above documents why it's expected.

## Testing

- `backend/tests/test_risk_engine.py`: a ferry-involving transfer (synthetic `RouteIndex` including a ferry route) resolves to `quality="insufficient"`, doesn't crash — proving the "no code change needed" claim above is actually true, not just reasoned about.
- `backend/tests/test_deadline.py`: an itinerary with a ferry leg makes `compute_deadline_threshold` return `None` — documents the deliberate all-or-nothing consequence as a real, asserted behavior.
- `backend/tests/test_nearest_stop.py` (or equivalent): a synthetic ferry stop is findable via `StopIndex.find_by_name` once `ferry.zip` is in the zip list.
- **Not unit-testable, inherently a live post-rebuild step**: whether OTP actually *routes* via ferry at all depends on the real rebuilt graph, which depends on a local Docker build this session cannot run. Completion gate matches every prior OTP-graph change in this project's history: a real `/chat` call planning a trip between two real NYC Ferry stops (or a stop pair where ferry is plausibly competitive with subway/bus), confirmed live after the rebuilt graph is deployed — not just "the code compiles."

## Explicitly out of scope

- A ferry reliability collector (real-time GTFS-RT ingestion → `arrival_events` → `reliability_buckets`) — confirmed with the user as a separate, future effort. The real, live-verified GTFS-RT endpoints are recorded above so that future work doesn't have to re-derive them.
- Fixing the pre-existing, unrelated `bus_gtfs/` gitignore gap at the repo root.
- Any change to `router-config.json` (no real-time ferry updater — there's nothing to update, since no reliability collector consumes it in this scope).
