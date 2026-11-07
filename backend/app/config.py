from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"
    # Not called anywhere yet -- Phase 3 Task 6 (replan_agent.py) deliberately
    # defers the design doc's aspirational Haiku-based multi-route-comparison
    # path (see task-6-brief.md's "Deliberately deferred" section: no test
    # exercises it, and this codebase doesn't ship untested LLM-decision
    # logic). Kept here, inert, so a future task that does build it has a
    # config field ready rather than a hardcoded model string (per the
    # Global Constraint that model choice is never hardcoded).
    replan_agent_model: str = "claude-haiku-4-5-20251001"
    # app/location_resolver.py's pre-geocoding query normalizer (2026-10-17,
    # user's explicit choice of Sonnet over Haiku here: resolving a vague/
    # colloquial NYC place reference into a geocoder-friendly search string
    # is a more judgment-heavy NLU task than this product's other Haiku-
    # handled steps). Sonnet, not Opus/flagship -- CLAUDE.md's model-tier
    # rule only bans Opus/flagship, not Sonnet.
    location_resolver_model: str = "claude-sonnet-5"
    # app/agents/location_quality_agent.py's post-geocode sanity check
    # (2026-11-06) -- classifying whether a candidate plausibly matches
    # the rider's description, not a judgment call needing Sonnet's
    # extra reasoning tier like location_resolver_model above.
    location_quality_model: str = "claude-haiku-4-5-20251001"
    db_path: str = str(Path(__file__).parent.parent / "data" / "risk.sqlite3")
    # Not read by any Python code path yet -- the deployment's uvicorn
    # invocation reads $PORT from the environment directly. Documented here
    # as the single source of truth for "which port does the backend
    # listen on," since both the public HTTP route and
    # REALTIME_PROXY_BASE_URL's Railway value depend on this same number
    # (Task 10 brief).
    port: int = 8000
    geoclient_api_key: str = ""
    # app/find_place.py's third-tier location fallback (after find_stop and
    # find_address both return no match) -- resolves a free-text business/
    # landmark name via Geoapify's Geocoding API. User-supplied 2026-10-17.
    geoapify_api_key: str = ""
    # Lets backend wake/stop the otp service on demand (app/otp_lifecycle.py,
    # 2026-10-11) via Railway's GraphQL API -- reuses the same personal
    # account token already used for this project's deploy/ops tooling
    # (user's explicit choice: simplicity over minting a narrower
    # project-scoped token, see CLAUDE.md's cost-incident notes for the
    # tradeoff). Empty locally/in tests -- otp_lifecycle's own calls are
    # mocked there, never real.
    railway_api_token: str = ""
    railway_project_id: str = "77096939-d30b-46a4-b439-c545aff3fe25"
    railway_otp_service_id: str = "7f4331b7-88ed-4688-8c76-b4c957017e1c"
    railway_environment_id: str = "95c28f31-9ee7-465f-9046-034215422795"
    # app/push.py's Web Push credentials (2026-11-06) -- generated once via
    # py_vapid, not rotated per-deploy. Empty locally/in tests (push.py's
    # send_push no-ops without a private key; the frontend's subscribe flow
    # simply has nothing to subscribe against without a public key).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # app/agents/feedback_triage_agent.py's two LLM steps (2026-11-06):
    # quality-check (is this real feedback?) and triage (categorize,
    # dedupe, summarize) are separate config fields per the model-choice
    # rule even though both default to the same model today -- either
    # could reasonably move to a different tier independently later.
    feedback_quality_model: str = "claude-haiku-4-5-20251001"
    feedback_triage_model: str = "claude-haiku-4-5-20251001"
    # app/github_issues.py's Feedback Triage Agent integration (2026-11-07)
    # -- the Issues-scoped fine-grained PAT rotated 2026-08-14 specifically
    # for this (see the credentials table above), finally used. Empty
    # locally/in tests -- github_issues.py's own functions no-op without
    # both set, same "not configured -- nothing to do" convention as
    # vapid_private_key above.
    github_token: str = ""
    github_repo_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
