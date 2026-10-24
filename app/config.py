"""Settings, read once from the environment (and a local .env in development)."""

from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "production"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

MIN_SECRET_KEY_LENGTH = 32
POSTGRES_SCHEMES = ("postgresql+psycopg://", "postgresql://", "postgres://")
DEV_DATABASE_URL = "postgresql+psycopg://el:el@127.0.0.1:5432/el_dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Environment = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    base_url: str = "http://localhost:8000"

    # Postgres. Supabase in production: use the Session pooler URI (IPv4, keeps prepared statements working).
    database_url: str = DEV_DATABASE_URL
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)

    # Signs session and CSRF tokens. Must be set (and long) outside dev and test.
    secret_key: SecretStr = SecretStr("dev-only-secret-key-change-me-0123456789")

    # Spend caps for the dollar-billed services, Apify and Claude (PRD, "Limits").
    run_budget_usd: Decimal = Field(default=Decimal("3.00"), gt=0)
    # The part of a run spent before approval. At least what analyze_site reserves, or no run could start.
    analysis_budget_usd: Decimal = Field(default=Decimal("0.50"), ge=Decimal("0.15"))
    daily_cap_usd: Decimal = Field(default=Decimal("20.00"), gt=0)

    # The background worker (ADR 0002).
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_heartbeat_seconds: int = Field(default=30, ge=1, le=300)
    worker_stale_after_seconds: int = Field(default=300, ge=60)  # no heartbeat for this long: the job is put back

    # Collecting posts (issue #5): one Apify search per event, whose posts other runs reuse for a few days.
    search_posts_per_query: int = Field(default=50, ge=1, le=200)
    search_cache_days: int = Field(default=3, ge=0, le=30)
    search_poll_seconds: int = Field(default=20, ge=1, le=600)

    # External services. "fake" runs everything offline on synthetic data (tests, local work, previews).
    providers: Literal["fake", "real"] = "fake"
    anthropic_api_key: SecretStr | None = None
    analyze_model: str = "claude-opus-5-5"
    classify_model: str = "claude-opus-5-5"  # claude-haiku-5-5 costs about 1/40 as much; the operator's call
    analyze_effort: Effort = "medium"
    classify_effort: Effort = "low"
    max_web_searches: int = Field(default=6, ge=1, le=20)
    llm_fallbacks: bool = True
    apify_token: SecretStr | None = None
    apify_actor: str = "harvestapi~linkedin-post-search"
    apify_posted_limit: Literal["week", "month", "3months", "6months"] = "3months"
    apollo_api_key: SecretStr | None = None
    reoon_enabled: bool = False
    reoon_api_key: SecretStr | None = None
    resend_api_key: SecretStr | None = None
    mail_from: str = "Event Leads <leads@theb2bdynamics.com>"

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """A blank variable (SECRET_KEY= in a copied .env, a CI secret that isn't set) means "use the default".

        A value of only spaces counts as blank. Except ENV: a blank ENV in a deployment must not quietly become "dev"
        and skip the checks below.
        """
        if not isinstance(data, dict):
            return data
        if "env" in data and _blank(data["env"]):
            raise ValueError("ENV is set but blank: set it to dev, test, staging or production")
        return {name: value for name, value in data.items() if not _blank(value)}

    @model_validator(mode="after")
    def _require_real_secrets(self) -> "Settings":
        if self.env in ("staging", "production"):
            key = self.secret_key.get_secret_value()
            if key.startswith("dev-only") or len(key) < MIN_SECRET_KEY_LENGTH:
                raise ValueError(
                    f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_KEY_LENGTH} characters"
                )
        if not self.database_url.startswith(POSTGRES_SCHEMES):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        if self.env in ("staging", "production") and self.sqlalchemy_url == DEV_DATABASE_URL:
            raise ValueError("DATABASE_URL must be set outside dev and test")
        if self.env == "production" and self.providers != "real":
            raise ValueError("production must use PROVIDERS=real")
        if self.analysis_budget_usd > self.run_budget_usd:
            raise ValueError("ANALYSIS_BUDGET_USD can't be more than RUN_BUDGET_USD")
        if self.worker_heartbeat_seconds * 3 > self.worker_stale_after_seconds:
            raise ValueError("WORKER_STALE_AFTER_SECONDS must be at least three heartbeats")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy needs the driver named; providers hand out plain postgresql:// (or legacy postgres://) URLs."""
        for plain in ("postgresql://", "postgres://"):
            if self.database_url.startswith(plain):
                return "postgresql+psycopg://" + self.database_url.removeprefix(plain)
        return self.database_url


def _blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
