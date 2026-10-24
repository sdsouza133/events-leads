"""Shared test setup.

Tests marked `db` run against a real Postgres (DATABASE_URL, default: the local el_test database). Locally they are
skipped when no database is reachable; in CI, REQUIRE_DB=1 turns that skip into a failure so they can't silently stop
running.
"""

import os
from collections.abc import Iterator

import pytest

# Set before importing the app: its settings are read from the environment.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://el:el@127.0.0.1:5432/el_test")

from app.config import Settings, get_settings
from app.db import database_is_reachable, get_engine, get_sessionmaker

# Tests never read a developer's .env: what they see comes from the variables set here and in each test.
Settings.model_config["env_file"] = None


def require_a_test_database(name: str | None) -> None:
    """Database tests create, empty and drop tables. Refuse to touch a database not named like one made for tests,
    so an exported DATABASE_URL (staging, production) can't be wiped by running pytest."""
    if not (name or "").endswith("_test"):
        raise pytest.UsageError(f"refusing to run database tests against {name!r}: its name must end in _test")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("db") for item in items):
        return
    if database_is_reachable():
        require_a_test_database(get_engine().url.database)
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
