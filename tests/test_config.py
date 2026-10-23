import pytest
from pydantic import ValidationError

from app.config import Settings


def test_dev_runs_with_defaults() -> None:
    settings = Settings(env="dev")
    assert settings.base_url == "http://localhost:8000"


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_environments_refuse_the_dev_secret(env: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(env=env)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_environments_refuse_a_short_secret(env: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(env=env, secret_key="too-short")


def test_production_accepts_a_long_random_secret() -> None:
    settings = Settings(env="production", secret_key="k" * 48)
    assert settings.env == "production"


def test_only_postgres_urls_are_accepted() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(database_url="sqlite:///local.db")


def test_plain_postgres_urls_get_the_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://u:p@db.example.com:5432/postgres")
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@db.example.com:5432/postgres"


def test_urls_that_already_name_the_driver_are_left_alone() -> None:
    url = "postgresql+psycopg://u:p@db.example.com:5432/postgres"
    assert Settings(database_url=url).sqlalchemy_url == url
