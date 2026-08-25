from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"
    mta_bustime_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
