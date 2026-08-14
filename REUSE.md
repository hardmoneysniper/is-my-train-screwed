# mini-nyc-3d Reuse Audit

**Audited:** 2026-08-14 · **Source:** `F:\Cornell Tech\mini-nyc-3d` (git remote: `hardmoneysniper/mini-nyc-3d`, MIT license, fork of `nagix/mini-tokyo-3d` © 2019-2026 Akihiko Kusanagi)

Per spec §0.2: this project's backend is Python; mini-nyc-3d is JS. Nothing below is ported as code — only **endpoint knowledge, field mappings, and quirks** are carried over, with provenance noted in comments at the port site.

**Attribution requirement:** MIT license requires the copyright notice to travel with any derived work. When we port logic (not code) this isn't strictly triggered, but if any code is ever copied verbatim (e.g. the direction-suffix regex), keep a comment citing `mini-nyc-3d src/loader.js` and the upstream MIT notice.

## Component-by-component

| mini-nyc-3d component | This project's need | Verdict |
|---|---|---|
| Subway GTFS-RT endpoint URLs (`src/configs.js:129-148`) | §4 real-time subway feed | **Reuse directly** — see below |
| GTFS-RT decode (`src/loader.js:136-252`, uses `gtfs-realtime-bindings`) | §0.2 "protobuf/NYCT-extension parsing" | **Do not port** — see below |
| Feed-polling cadence (`configs.js` `refreshInterval`/`realtimeCheckInterval`) | Collector polling cadence | **Do not reuse** — UI-tuned, not collection-tuned |
| Retry logic (`loader.js:153-156`) | Collector reliability | **Do not reuse** — none exists to reuse |
| Stop/route ID conventions (`loader.js:175-209`) | Risk Engine bucket keys, trip matching | **Reuse the pattern** |
| Static GTFS handling (`scripts/convert-mta-gtfs.js`) | §0.1.2 GTFS download loop | **Partial reuse + one flag** — see below |
| Bus data access (`loader.js:309-341`, `src/data-classes/bus.js`) | §5.2 Bus Observation Collection | **No SIRI code exists; GTFS-RT decoder pattern is reusable** — see below |

### 1. Subway GTFS-RT — reuse directly

Base URL: `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/`, feed paths appended as `nyct%2Fgtfs-ace`, `nyct%2Fgtfs-bdfm`, `nyct%2Fgtfs-g`, `nyct%2Fgtfs-jz`, `nyct%2Fgtfs-nqrw`, `nyct%2Fgtfs-l`, `nyct%2Fgtfs` (numbered lines 1-7 + S), `nyct%2Fgtfs-si`. **No API key required** — confirmed both in mini-nyc-3d's code comment and by a live `curl` smoke test (HTTP 200) on 2026-08-14. Port these 8 paths verbatim into the Python collector's feed list.

### 2. Protobuf/NYCT-extension parsing — do not port, use `nyct-gtfs` instead

mini-nyc-3d's decode (`loader.js:136-252`) only reads **standard** GTFS-RT `FeedMessage` fields (`vehicle`, `tripUpdate`, `alert`) via the generic `gtfs-realtime-bindings` library. It does **not** decode the NYCT protobuf extension (actual-track assignment, train ID reassignment, scheduled-vs-actual routing) that the spec's `arrival_events.derivation_quality` and delay computation will likely need for the more careful passage-detection state machine in §5.2. Recommendation: use the Python `nyct-gtfs` library (already named as acceptable in spec §4) rather than hand-rolling a decoder from this JS as a starting point — it's a strictly bigger feature set than what's here.

**Worth reusing regardless:** the direction-suffix normalization quirk — subway stop IDs carry a trailing `N`/`S` platform-direction suffix that must be stripped for a stop-level (not platform-level) identity: `stopId.replace(/[NS]$/, '')` (`loader.js:175,209`). Port this rule into the Python stop-ID normalizer; it's a real MTA data quirk, easy to miss, and would otherwise double-count platforms as separate stops in `reliability_buckets`.

**Also reusable:** entity → internal ID shape `MTA.{service}.{tripId}` and route ID shape `MTA.{service}.{routeId}` (`loader.js:174-177`) — not needed verbatim, but confirms `service`+`tripId` is a sufficient composite key, consistent with the spec's `agency, route_id, ...` schema.

### 3. Polling cadence & retry — do not reuse, build new

`configs.js` cadences (`refreshInterval: 60000`, `realtimeCheckInterval: 15000`) govern train-animation smoothness in a live map UI, not data-collection completeness — wrong optimization target for the Risk Engine's collector. Retry logic is a single `fetch().catch()` that logs a warning and substitutes an empty entity list (`loader.js:153-156`) — there is no backoff/retry to reuse. The spec's own cadences (§5.2: bus poll every 30s; §6: monitor poll every 60s) should be implemented fresh in the Python collectors, with real retry/backoff added (none of this class of logic exists anywhere in mini-nyc-3d to port).

### 4. Static GTFS — partial reuse, one conflict to flag

`scripts/convert-mta-gtfs.js` downloads **LIRR and MNR** static GTFS live from `https://web.mta.info/developers/data/{lirr,mnr}/google_transit.zip` (out of scope for this project per spec §1) but reads **Subway** from a checked-in local snapshot (`data/gtfs_subway/`) rather than a live URL, specifically to avoid `shape_id` collisions between a fresh trips.txt and a stale local shapes.txt.

**[FLAG per §0.2.4]** This means mini-nyc-3d does not currently reveal a confirmed *live* URL for subway static GTFS — it was downloaded once, out-of-band, and pinned locally. Before Phase 1, confirm the current subway static GTFS zip URL directly against the MTA developer portal (candidate, unverified by this audit: `https://web.mta.info/developers/data/nyct/subway/google_transit.zip`, following the same `web.mta.info/developers/data/{agency}/google_transit.zip` pattern as the confirmed-live LIRR/MNR URLs) and decide whether to pin a local snapshot (mini-nyc-3d's approach, avoids shape_id churn) or live-download each build (spec's implied default). Recommend pinning with a documented refresh cadence — same shape_id-collision risk applies here.

### 5. Bus — no SIRI code exists; a GTFS-RT decoder pattern does

mini-nyc-3d has **no MTA Bus Time / SIRI StopMonitoring integration at all.** What it has:
- `loadDynamicBusData(url)` (`loader.js:338-342`) — a *generic* GTFS-RT `FeedMessage` protobuf decoder (same `gtfs-realtime-bindings` lib as subway), decoupled from any specific bus endpoint; the caller supplies the URL.
- `loadBusData()` (`loader.js:309-336`) + `src/helpers/helpers-gtfs.js` — static bus route/stop/trip data, custom-packed via a hand-rolled protobuf schema (not GTFS-RT; this is mini-nyc-3d's own on-disk format for the map UI, not reusable for a backend collector).
- `src/data-classes/bus.js` — a thin UI position/animation object (route, offset along shape), not a data-access component.

**Implication for §5.2:** MTA Bus Time actually publishes real-time bus data two ways — SIRI `StopMonitoring` (what the spec names) and a separate GTFS-RT bus feed. mini-nyc-3d's `loadDynamicBusData` pattern confirms the GTFS-RT bus path is viable and requires only a URL (no SIRI-specific XML/JSON parsing). **Smoke-tested 2026-08-14:** `https://gtfsrt.prod.obanyc.com/tripUpdates?key=<BUSTIME_KEY>` returned HTTP 200 with Frank's key. Recommendation to flag to Frank: consider GTFS-RT as the bus collector's primary source instead of SIRI `StopMonitoring` — it reuses the exact same protobuf decode path already proven for subway (one decoder, two agencies, matches spec §5.2's "one agency-agnostic schema" decision), whereas SIRI would need a second, XML/JSON-based parser built from scratch. Either way, the passage-detection state machine (§5.2 "approaching → past" transition) must be written new — nothing in mini-nyc-3d does this.

## Summary for Frank

- **Reuse as-is:** subway GTFS-RT endpoint list + no-key confirmation; stop-ID `N`/`S` suffix-stripping quirk.
- **Reuse the pattern, not the code:** GTFS-RT protobuf decoding via a generic library (points toward GTFS-RT over SIRI for bus, pending your call).
- **Build new (nothing to port):** collector polling/retry/backoff, NYCT-extension parsing (use `nyct-gtfs`), arrival-event passage-detection state machine, bus real-time integration of any kind.
- **Needs your decision:** GTFS-RT vs. SIRI for bus collection (§5.2 doesn't mandate one); live-download vs. pinned-snapshot for subway static GTFS.
