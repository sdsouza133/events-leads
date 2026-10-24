from pathlib import Path

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


PROD_DB = "postgresql://u:p@db.example.com:5432/postgres"


def test_production_accepts_a_long_random_secret() -> None:
    settings = Settings(env="production", secret_key="k" * 48, database_url=PROD_DB, providers="real")
    assert settings.env == "production"


def test_production_refuses_to_fall_back_to_the_dev_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL must be set"):
        Settings(env="production", secret_key="k" * 48, providers="real")


@pytest.mark.parametrize(
    "spelling",
    ["postgresql://el:el@127.0.0.1:5432/el_dev", "postgres://el:el@127.0.0.1:5432/el_dev"],
)
def test_production_refuses_the_dev_database_however_it_is_spelled(spelling: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL must be set"):
        Settings(env="production", secret_key="k" * 48, database_url=spelling)


def test_only_postgres_urls_are_accepted() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(database_url="sqlite:///local.db")


def test_plain_postgres_urls_get_the_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://u:p@db.example.com:5432/postgres")
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@db.example.com:5432/postgres"


def test_legacy_postgres_scheme_is_accepted_and_converted() -> None:
    settings = Settings(database_url="postgres://u:p@db.example.com:5432/postgres")
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@db.example.com:5432/postgres"


def test_a_blank_secret_falls_back_to_the_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "")
    assert Settings(env="dev").secret_key.get_secret_value().startswith("dev-only")


def test_an_unset_env_means_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV", raising=False)
    assert Settings().env == "dev"


def test_a_blank_env_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Falling back to "dev" here would skip the staging and production checks.
    monkeypatch.setenv("ENV", "")
    with pytest.raises(ValidationError, match="ENV is set but blank"):
        Settings()


def test_tests_never_read_a_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("BASE_URL=https://from-a-dotenv.example\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BASE_URL", raising=False)
    assert Settings().base_url == "http://localhost:8000"


def test_urls_that_already_name_the_driver_are_left_alone() -> None:
    url = "postgresql+psycopg://u:p@db.example.com:5432/postgres"
    assert Settings(database_url=url).sqlalchemy_url == url
