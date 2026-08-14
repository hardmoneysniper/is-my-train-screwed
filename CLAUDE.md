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

| Credential | Status | Env var |
|---|---|---|
| MTA BusTime API key | ✅ have it, smoke-tested live (HTTP 200 on GTFS-RT bus endpoint) | `MTA_BUSTIME_API_KEY` |
| Anthropic API key | ❌ not yet provided — blocks live/eval testing of any agent (code can still be written+unit-tested with mocks) | `ANTHROPIC_API_KEY` |
| Email API key (Resend/Postmark) | ❌ not yet provided — blocks Phase 4 only | `RESEND_API_KEY` or `POSTMARK_API_KEY` |
| GitHub token (Feedback Triage issue filing) | ❌ not yet provided — blocks Phase 4 only | `GITHUB_TOKEN` |
| Hosting account (Fly.io/Railway) | ❌ not yet provided — blocks public deploy only, not local dev | n/a |
| MTA subway GTFS-RT | ✅ confirmed live, no key required: `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/{feed}` | n/a |
| MTA subway/bus static GTFS zip URL | ⚠️ unconfirmed — mini-nyc-3d pins a local snapshot rather than a live URL; candidate pattern is `web.mta.info/developers/data/{agency}/google_transit.zip`, verify before relying on it long-term (see `REUSE.md` §4) | n/a |
| subwaydata.nyc backfill | ✅ confirmed live, data back to 2021-04-01, daily ~7am | n/a |

Before touching credentials/keys in code or docs, re-check this table is still current — it's a snapshot, not a live source.

## Reminders for future sessions

- Re-verify MTA endpoint URLs against current MTA developer documentation if it's been a while — they've changed before and the spec explicitly warns not to trust training data here.
- Follow the frontend-design skill before/while building any UI — this product must not look like a default AI chat app (spec §9).
- Any model-routing change must be justified against the `evals/` fixture set (~30 anonymized transcripts) comparing tool-call accuracy across models — no routing changes on vibes (spec §9.1).
