"""App settings, loaded from environment variables (or a local .env file)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    claude_model: str = ""
    search_endpoint: str = ""
    search_api_key: str = ""
    search_index_name: str = "dsu-content"
    allowed_origins: str = "http://localhost:8000"
    log_level: str = "INFO"


settings = Settings()
