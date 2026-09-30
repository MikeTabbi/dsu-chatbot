"""App settings, loaded from environment variables (or a local .env file)."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: a blank line like CLAUDE_MODEL= keeps the default instead of setting ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    anthropic_api_key: SecretStr = SecretStr("")  # SecretStr: shows as ********** if printed
    claude_client: str = "fake"  # fake (canned answers, no key) | anthropic | azure (#19)
    claude_model: str = "claude-opus-5"  # placeholder until the model is chosen in #19
    claude_max_output_tokens: int = 4096
    claude_timeout_seconds: float = 30.0
    search_endpoint: str = ""
    search_api_key: str = ""
    search_index_name: str = "dsu-content"
    retriever: str = "local"  # local (keyword search over chunks_dir) | azure (not built yet)
    chunks_dir: str = "data/chunks"
    retriever_min_score: float = 0.1
    allowed_origins: str = "http://localhost:8000"
    log_level: str = "INFO"


settings = Settings()
