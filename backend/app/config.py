from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"
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
