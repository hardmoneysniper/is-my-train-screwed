# Is My Train Screwed?

Conversational NYC transit trip advisor (PWA). Full spec: `is-my-train-screwed-spec.md`. Project context, credentials status, and build history: `CLAUDE.md`.

## Known deferrals

- **Transfer-risk precomputation for M→Q70 (weekday) and R→Q70 (weekend).** Decision 2026-08-29: `get_risk` computes transfer-miss probability live at query time by default (spec §5.2's intended design — Monte Carlo over pre-aggregated histograms, expected sub-10ms). For these two specific, high-traffic transfer patterns, precompute the result instead of relying on the live path, as an extra safety margin. **Not implemented yet** — needs at least 30 days of real observation data to be worth running (bus collector started 2026-08-15; subwaydata.nyc backfill not yet ingested). Revisit once that threshold is reached: add a small precomputed-result check ahead of the live path for just these two `(route_pair, day_type)` keys, not a general cache for all transfers.
