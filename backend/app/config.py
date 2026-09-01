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
    db_path: str = str(Path(__file__).parent.parent / "data" / "risk.sqlite3")
    # Not read by any Python code path yet -- the deployment's uvicorn
    # invocation reads $PORT from the environment directly. Documented here
    # as the single source of truth for "which port does the backend
    # listen on," since both the public HTTP route and
    # REALTIME_PROXY_BASE_URL's Railway value depend on this same number
    # (Task 10 brief).
    port: int = 8000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
