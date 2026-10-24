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


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_secret_falls_back_to_the_dev_default(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("SECRET_KEY", blank)
    assert Settings(env="dev").secret_key.get_secret_value().startswith("dev-only")


def test_an_unset_env_means_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV", raising=False)
    assert Settings().env == "dev"


@pytest.mark.parametrize("blank", ["", "  "])
def test_a_blank_env_is_refused(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    # Falling back to "dev" here would skip the staging and production checks.
    monkeypatch.setenv("ENV", blank)
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


def test_the_analysis_budget_fits_inside_the_run_budget() -> None:
    with pytest.raises(ValidationError, match="ANALYSIS_BUDGET_USD"):
        Settings(run_budget_usd="1.00", analysis_budget_usd="2.00")


def test_a_job_gets_several_heartbeats_before_it_counts_as_stale() -> None:
    with pytest.raises(ValidationError, match="three heartbeats"):
        Settings(worker_heartbeat_seconds=60, worker_stale_after_seconds=120)


@pytest.mark.parametrize(
    ("raw", "statuses"),
    [
        ("verified", ("verified",)),
        ("Verified, likely to engage ,verified", ("verified", "likely to engage")),
        (["Verified"], ("verified",)),
    ],
)
def test_accepted_email_statuses_are_a_comma_separated_list(raw: object, statuses: tuple[str, ...]) -> None:
    assert Settings(accepted_email_statuses=raw).accepted_email_statuses == statuses


def test_accepted_email_statuses_come_from_the_environment_as_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCEPTED_EMAIL_STATUSES", "verified,likely to engage")
    assert Settings().accepted_email_statuses == ("verified", "likely to engage")


def test_at_least_one_email_status_is_accepted() -> None:
    with pytest.raises(ValidationError, match="accepted_email_statuses"):
        Settings(accepted_email_statuses=" , ")


def test_people_are_not_cached_past_the_30_days_personal_data_is_kept() -> None:
    with pytest.raises(ValidationError, match="person_cache_days"):
        Settings(person_cache_days=31)


def test_a_list_holds_at_most_50_people() -> None:
    with pytest.raises(ValidationError, match="max_leads"):
        Settings(max_leads=51)
