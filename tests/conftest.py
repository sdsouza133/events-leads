"""Shared test setup.

Tests marked `db` run against a real Postgres (DATABASE_URL, default: the local el_test database). Locally they are
skipped when no database is reachable; in CI, REQUIRE_DB=1 turns that skip into a failure so they can't silently stop
running.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

# Set before importing the app: its settings are read from the environment.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://el:el@127.0.0.1:5432/el_test")

from app.config import get_settings
from app.db import database_is_reachable, get_engine, get_sessionmaker
from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("db") for item in items):
        return
    if database_is_reachable():
        return
    if os.environ.get("REQUIRE_DB") == "1":
        raise pytest.UsageError("REQUIRE_DB=1 but the database at DATABASE_URL is not reachable")
    skip = pytest.mark.skip(reason="no Postgres reachable at DATABASE_URL")
    for item in items:
        if item.get_closest_marker("db"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Each test sees the environment as it is now, not as a previous test left the cached settings."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _dispose_engine() -> Iterator[None]:
    yield
    get_engine().dispose()
    get_sessionmaker.cache_clear()


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def migrated() -> None:
    """The schema at the latest migration, built once per test session."""
    command.upgrade(alembic_config(), "head")


def truncate_all_tables() -> None:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db(migrated: None) -> Iterator[Session]:
    """A session on the migrated test database; every table is emptied afterwards."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        truncate_all_tables()
