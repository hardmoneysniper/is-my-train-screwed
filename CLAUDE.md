# Is My Train Screwed? — Project Context

Full spec: `is-my-train-screwed-spec.md` (read it for anything not covered below — this file is orientation, not a substitute).

## What this is

Conversational NYC transit trip advisor (PWA). Differentiates on judgment, not routing: empirical (never LLM-guessed) transfer-miss probabilities, proactive monitored-trip re-planning, accessibility/luggage-aware routing, and a data-gated airport mode (LGA first). Beachhead users: Cornell Tech / Roosevelt Island subway commuters.

## Hard rules (violating these is a spec bug, not a style nit)

- **LLM agents never compute numbers, routes, or probabilities.** They call tools and narrate tool output only. If you catch yourself writing a prompt that asks a model to produce a number, stop — the fix is always better data or an honest "insufficient data (n<200)" response.
- **Model choice is a config/env parameter per agent, never hardcoded.** Default Haiku 4.5 everywhere; never Opus/flagship tier for anything in this product.
- Bus corridor probabilities/airport mode only surface once that corridor's data crosses `n≥200` observations — data-gated, not date-gated.
- The only autonomous write path any agent has is appending a validated entry to bus-corridor collector config (Coverage-Expansion Agent). Everything else escalates to Frank.
- Never mock/stub/hardcode fake credentials "to keep moving." If something's missing, build what doesn't depend on it and flag what's blocked — don't fake it.

## Reference documents (read these before re-deriving their contents)

- `REUSE.md` — mini-nyc-3d (`F:\Cornell Tech\mini-nyc-3d`) reuse audit. Confirmed-live subway GTFS-RT endpoint + feed paths, the `N`/`S` stop-suffix quirk, and the finding that mini-nyc-3d has **no bus real-time integration** to port (a GTFS-RT decode pattern exists and is reusable; SIRI has nothing to port).
- `docs/superpowers/plans/2026-08-14-is-my-train-screwed-roadmap.md` — Phase 1 bite-sized implementation plan (file-level, TDD, complete code) plus Phase 2-5 roadmap scope. Written via the superpowers:writing-plans skill; execute via subagent-driven-development or executing-plans.

## Credentials status (as of 2026-08-14)

All secrets live in `backend/.env` (gitignored, never committed — verified `git status` excludes it before every commit).

| Credential | Status | Env var |
|---|---|---|
| MTA BusTime API key | ✅ have it, live-tested (HTTP 200 on all 3 GTFS-RT bus endpoints: tripUpdates, vehiclePositions, alerts) | `MTA_BUSTIME_API_KEY` |
| Anthropic API key | ✅ have it, live-tested against `claude-haiku-4-5-20251001` (cheapest model — use this everywhere per user instruction, matches spec §9.1 default anyway) | `ANTHROPIC_API_KEY` |
| GitHub token | ✅ have it, verified `push`+`admin` on `hardmoneysniper/is-my-train-screwed` (empty repo, now has initial commit on `main`). **Broader than needed** — the Feedback Triage Agent (Phase 4) should get its own narrower token (Issues: read/write only, see chat) rather than reuse this one when that phase starts. | `GITHUB_TOKEN` |
| Email API key (Resend/Postmark) | ❌ not yet provided — user will supply after hosting is live. Blocks Phase 4 only | `RESEND_API_KEY` or `POSTMARK_API_KEY` |
| Hosting account (Railway free tier) | ❌ not yet provided — user will supply after hosting is live. Blocks public deploy only, not local dev/collection | n/a |
| MTA subway GTFS-RT | ✅ confirmed live, no key required: `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/{feed}` | n/a |
| MTA bus GTFS-RT | ✅ confirmed live 2026-08-14: `https://gtfsrt.prod.obanyc.com/{tripUpdates,vehiclePositions,alerts}?key=...`. Real route_id values confirmed: `Q70+`, `M60+` (the `+` = SBS branding). **Chosen over SIRI StopMonitoring** — see `REUSE.md` §5 | n/a |
| MTA subway/bus static GTFS zip URL | ⚠️ still unconfirmed — mini-nyc-3d pins a local snapshot rather than a live URL; candidate pattern is `web.mta.info/developers/data/{agency}/google_transit.zip`, verify before relying on it long-term (see `REUSE.md` §4). Not blocking the bus collector; blocks OTP setup (Phase 1 Task 2) | n/a |
| subwaydata.nyc backfill | ✅ confirmed live, data back to 2021-04-01, daily ~7am | n/a |

**Anthropic spend cap:** user requested a **$5/month hard cap**. There is no API to set this — **user must set it manually** at console.anthropic.com under Settings > Limits; I cannot verify it's been set. As a second line of defense, `backend/cost_guard.py` logs every call's cost locally and (a) raises before further calls once month-to-date spend hits the cap, (b) prints an alarm if a single day's spend implies burning the budget 3x+ faster than sustainable. This is a local circuit breaker only — it does not replace the console cap, which is the authoritative stop.

Before touching credentials/keys in code or docs, re-check this table is still current — it's a snapshot, not a live source.

## Build order (reprioritized 2026-08-14 — deviates from the roadmap plan's Task 1-7 order)

User explicitly prioritized bus corridor data collection over the rest of Phase 1, since bus reliability has no retroactive historical source (subway does, via subwaydata.nyc) and needs ~2-3 weeks of live observation before any probability is meaningful. Actual build order:

1. **Done:** `backend/collectors/bus_collector.py` — polls Q70+/M60+ GTFS-RT every 30s, writes raw ndjson to `backend/data/raw/bus/YYYY-MM-DD.ndjson` (gitignored, local only), retry+backoff on failure. Running in background as of 2026-08-14.
2. **Important — not yet solved:** the collector currently only runs inside an ephemeral coding-session background process. For genuine unattended 2-3 week collection it needs to run as a real persistent process (Windows Task Scheduler, a small always-on VPS/Railway service once that's set up, or at minimum a terminal window that stays open) — flag this to the user if a session ends and collection needs to keep going.
3. Original Task 1-7 (FastAPI scaffold, OTP, nearest-stop index, PWA shell) resumes after the collector is confirmed stable — not blocked by anything except the static GTFS URL confirmation (Task 2).

## Reminders for future sessions

- Re-verify MTA endpoint URLs against current MTA developer documentation if it's been a while — they've changed before and the spec explicitly warns not to trust training data here.
- Follow the frontend-design skill before/while building any UI — this product must not look like a default AI chat app (spec §9).
- Any model-routing change must be justified against the `evals/` fixture set (~30 anonymized transcripts) comparing tool-call accuracy across models — no routing changes on vibes (spec §9.1).
