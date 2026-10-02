"""App settings, loaded from environment variables (or a local .env file)."""

from pydantic import SecretStr, field_validator
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
    synonyms_file: str = "api/app/synonyms.yaml"  # word groups for the local retriever
    chat_top_k: int = 5  # chunks retrieved per question and passed to Claude
    chat_max_question_chars: int = 1000
    # Sites allowed to call the API from a browser (the widget), comma-separated. The default is
    # the local demo page (widget/README.md); production lists DSU's site. "*" is refused.
    allowed_origins: str = "http://localhost:8080,http://127.0.0.1:8080"
    log_level: str = "INFO"

    @field_validator("allowed_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("ALLOWED_ORIGINS must list exact sites; '*' (any site) isn't allowed")
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
