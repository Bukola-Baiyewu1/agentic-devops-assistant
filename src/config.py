"""Central, validated configuration.

Every value can be overridden with an environment variable. `settings` is the
single object the rest of the code reads from. In production mode the
application refuses to start with insecure defaults (see `validate_for_startup`).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env without overriding real environment variables."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip().removeprefix("export ").strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    """An environment variable, treating an empty value as 'not set'."""
    return os.getenv(name) or default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


class Settings(BaseModel):
    # --- runtime -----------------------------------------------------------
    env: str = Field(default="development", description="development | production")
    database_url: str = "sqlite:///./.aegis.db"
    runbooks_dir: str = "runbooks"
    secret_key: str = "dev-insecure-secret-change-me"  # noqa: S105 - rejected in production

    # --- planner -----------------------------------------------------------
    llm_provider: str = "auto"  # auto | mock | anthropic
    anthropic_api_key: str = ""
    agent_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 1024
    max_plan_iterations: int = 4
    min_confidence: float = 0.5
    # Illustrative USD prices per million tokens. These are ESTIMATES used for
    # dashboards, not billing truth. Set them from the provider's price page.
    llm_input_price_per_mtok: float = 3.0
    llm_output_price_per_mtok: float = 15.0

    # --- retrieval ---------------------------------------------------------
    retrieval_top_k: int = 3
    retrieval_min_score: float = 0.08

    # --- policy ------------------------------------------------------------
    allowed_services: list[str] = ["web"]
    max_replicas: int = 5

    # --- approvals ---------------------------------------------------------
    approval_ttl_seconds: int = 900
    capability_ttl_seconds: int = 300
    # "name:password" pairs, comma separated. Passwords may be given as
    # "sha256:<hex>" so plain text never needs to be stored.
    users: str = ""

    # --- ingress -----------------------------------------------------------
    webhook_secret: str = ""
    rate_limit_per_minute: int = 120
    process_inline: bool = True

    # --- retries -----------------------------------------------------------
    max_event_attempts: int = 5
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 300.0
    worker_poll_seconds: float = 1.0

    # --- observability -----------------------------------------------------
    metrics_token: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    def planner_mode(self) -> str:
        if self.llm_provider == "auto":
            return "anthropic" if self.anthropic_api_key else "mock"
        return self.llm_provider

    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def load_settings() -> Settings:
    return Settings(
        env=_env("AEGIS_ENV", "development"),
        database_url=_env("DATABASE_URL", "sqlite:///./.aegis.db"),
        runbooks_dir=_env("RUNBOOKS_DIR", "runbooks"),
        secret_key=_env("AEGIS_SECRET_KEY", "dev-insecure-secret-change-me"),
        llm_provider=_env("AEGIS_LLM_PROVIDER", "auto"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        agent_model=_env("AGENT_MODEL", "claude-sonnet-5-5"),
        llm_timeout_seconds=float(_env("AEGIS_LLM_TIMEOUT_SECONDS", "30")),
        llm_max_tokens=int(_env("AEGIS_LLM_MAX_TOKENS", "1024")),
        max_plan_iterations=int(_env("MAX_PLAN_ITERATIONS", "4")),
        min_confidence=float(_env("AEGIS_MIN_CONFIDENCE", "0.5")),
        llm_input_price_per_mtok=float(_env("AEGIS_LLM_INPUT_PRICE_PER_MTOK", "3.0")),
        llm_output_price_per_mtok=float(_env("AEGIS_LLM_OUTPUT_PRICE_PER_MTOK", "15.0")),
        retrieval_top_k=int(_env("AEGIS_RETRIEVAL_TOP_K", "3")),
        retrieval_min_score=float(_env("AEGIS_RETRIEVAL_MIN_SCORE", "0.08")),
        allowed_services=_csv("AEGIS_ALLOWED_SERVICES", "web"),
        max_replicas=int(_env("AEGIS_MAX_REPLICAS", "5")),
        approval_ttl_seconds=int(_env("AEGIS_APPROVAL_TTL_SECONDS", "900")),
        capability_ttl_seconds=int(_env("AEGIS_CAPABILITY_TTL_SECONDS", "300")),
        users=_env("AEGIS_USERS"),
        webhook_secret=_env("AEGIS_WEBHOOK_SECRET"),
        rate_limit_per_minute=int(_env("AEGIS_RATE_LIMIT_PER_MINUTE", "120")),
        process_inline=_bool("AEGIS_PROCESS_INLINE", True),
        max_event_attempts=int(_env("AEGIS_MAX_EVENT_ATTEMPTS", "5")),
        retry_base_seconds=float(_env("AEGIS_RETRY_BASE_SECONDS", "2")),
        retry_max_seconds=float(_env("AEGIS_RETRY_MAX_SECONDS", "300")),
        worker_poll_seconds=float(_env("AEGIS_WORKER_POLL_SECONDS", "1")),
        metrics_token=_env("AEGIS_METRICS_TOKEN"),
        langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_env("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=_env("LANGFUSE_BASE_URL"),
    )


load_dotenv(os.getenv("AEGIS_DOTENV", ".env"))
settings = load_settings()


class ConfigError(RuntimeError):
    """Raised when the configuration is unsafe for the selected environment."""


def validate_for_startup(s: Settings | None = None) -> None:
    """Fail closed: production mode must not run with development defaults."""
    s = s or settings
    if not s.is_production:
        return
    problems = []
    if s.secret_key.startswith("dev-") or len(s.secret_key) < 32:
        problems.append("AEGIS_SECRET_KEY must be a random value of at least 32 characters")
    if not s.webhook_secret:
        problems.append("AEGIS_WEBHOOK_SECRET is required to verify alert senders")
    if not s.users:
        problems.append("AEGIS_USERS must define at least one approver")
    if s.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL must point to PostgreSQL in production")
    if problems:
        raise ConfigError("; ".join(problems))
