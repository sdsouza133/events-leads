"""Settings, read once from the environment (and a local .env in development)."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "production"]

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
