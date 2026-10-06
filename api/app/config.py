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
    judge_model: str = ""  # the eval's Claude grader (eval/judge.py); blank: CLAUDE_MODEL
    search_endpoint: str = ""
    search_api_key: str = ""
    search_index_name: str = "dsu-content"
    retriever: str = "local"  # local (keyword search over index_path) | azure (not built yet)
    # The local index the ingestion pipeline syncs; the local retriever reloads it when it changes.
    index_path: str = "data/index/chunks.json"
    retriever_min_score: float = 0.1
    synonyms_file: str = "api/app/synonyms.yaml"  # word groups for the local retriever
    chat_top_k: int = 5  # chunks retrieved per question and passed to Claude
    chat_max_question_chars: int = 1000
    # Sites allowed to call the API from a browser (the widget), comma-separated. The default is
    # the local demo page (widget/README.md); production lists DSU's site. "*" is refused.
    allowed_origins: str = "http://localhost:8080,http://127.0.0.1:8080"
    log_level: str = "INFO"
    # Every /chat exchange and its feedback, PII redacted (api/app/exchange_log.py).
    exchange_log: str = "sqlite"  # sqlite (a local file) | none (keep nothing)
    exchange_log_path: str = "data/exchanges/exchanges.sqlite"  # data/ is gitignored
    exchange_retention_days: int = 90  # the purge command deletes exchanges older than this
    feedback_max_comment_chars: int = 500
    # Abuse protection (api/app/rate_limit.py). Per-client limits count by IP; 0 turns one off.
    rate_limiter: str = "memory"  # memory (counters in this process) | none (no limits)
    rate_limit_chat_per_minute: int = 10
    rate_limit_chat_per_day: int = 100
    rate_limit_feedback_per_minute: int = 30
    # Claude calls per UTC day across all clients; past it /chat says it's busy. 0: no budget.
    claude_daily_call_budget: int = 2000
    # Read the client IP from X-Forwarded-For. Only turn on behind a proxy that sets it (Azure
    # App Service); otherwise anyone could send the header and pick their own IP.
    trust_proxy: bool = False
    max_request_bytes: int = 16384  # largest /chat or /feedback body; bigger gets a 413

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
