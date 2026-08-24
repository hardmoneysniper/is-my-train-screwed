from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"

    class Config:
        env_file = ".env"


settings = Settings()
