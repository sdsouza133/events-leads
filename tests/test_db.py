from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import get_engine, session_scope
from app.models import Base

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def scratch_table() -> Iterator[str]:
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS _scratch (v int)"))
        conn.execute(text("TRUNCATE _scratch"))
    yield "_scratch"
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS _scratch"))


def _count(table: str) -> int:
    with get_engine().connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608 (fixed name)


def test_session_scope_commits_on_success(scratch_table: str) -> None:
    with session_scope() as session:
        session.execute(text(f"INSERT INTO {scratch_table} VALUES (1)"))  # noqa: S608 (fixed name)
    assert _count(scratch_table) == 1


def test_session_scope_rolls_back_on_error(scratch_table: str) -> None:
    def insert_then_fail() -> None:
        with session_scope() as session:
            session.execute(text(f"INSERT INTO {scratch_table} VALUES (1)"))  # noqa: S608 (fixed name)
            raise RuntimeError("fail mid-transaction")

    with pytest.raises(RuntimeError, match="mid-transaction"):
        insert_then_fail()
    assert _count(scratch_table) == 0


def test_migrations_apply_and_match_the_models() -> None:
    """`alembic check` fails when a model changed without a migration, so CI catches a forgotten one."""
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)


def test_constraint_names_follow_the_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
