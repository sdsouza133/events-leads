"""Database engine and sessions.

One engine per process. Web requests and worker jobs each open a short-lived session with `session_scope()`.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, Result, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.sqlalchemy_url,
        hide_parameters=True,  # a failed statement's message would otherwise quote its values (emails, say)
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # poolers drop idle connections; check before use instead of failing a request
        pool_recycle=1800,
        # Without this, an unreachable database blocks connect() until the OS gives up (minutes), tying up the
        # threads that /readyz and every other request run on.
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on any error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def database_is_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # The probe only answers yes or no; the reason goes to the logs.
        log.warning("database not reachable", exc_info=True)
        return False
    return True


def rowcount(result: Result[Any]) -> int:
    """Rows touched by an UPDATE or DELETE. SQLAlchemy types `Session.execute` as a plain Result, but for those
    statements it is a CursorResult, which carries the count."""
    return int(cast(CursorResult[Any], result).rowcount or 0)
