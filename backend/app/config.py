from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"
    db_path: str = str(Path(__file__).parent.parent / "data" / "risk.sqlite3")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
