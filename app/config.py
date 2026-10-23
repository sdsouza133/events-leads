"""Settings, read once from the environment (and a local .env in development)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "production"]

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Environment = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    base_url: str = "http://localhost:8000"

    # Postgres. Supabase in production: use the Session pooler URI (IPv4, keeps prepared statements working).
    database_url: str = "postgresql+psycopg://el:el@127.0.0.1:5432/el_dev"
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)

    # Signs session and CSRF tokens. Must be set (and long) outside dev and test.
    secret_key: SecretStr = SecretStr("dev-only-secret-key-change-me-0123456789")

    @model_validator(mode="after")
    def _require_real_secrets(self) -> "Settings":
        if self.env in ("staging", "production"):
            key = self.secret_key.get_secret_value()
            if key.startswith("dev-only") or len(key) < MIN_SECRET_KEY_LENGTH:
                raise ValueError(
                    f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_KEY_LENGTH} characters"
                )
        if not self.database_url.startswith(("postgresql+psycopg://", "postgresql://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy needs the driver named; Supabase and Railway hand out plain postgresql:// URLs."""
        if self.database_url.startswith("postgresql://"):
            return "postgresql+psycopg://" + self.database_url.removeprefix("postgresql://")
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
