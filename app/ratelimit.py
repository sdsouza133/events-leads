"""Fixed-window rate limits stored in Postgres, so they hold across every web replica."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import rowcount
from app.models import RateLimit


def window_start(now: datetime, window: timedelta) -> datetime:
    seconds = int(window.total_seconds())
    if seconds <= 0:
        raise ValueError("window must be at least one second")
    return datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, UTC)


def hit(session: Session, key: str, *, limit: int, window: timedelta, now: datetime | None = None) -> bool:
    """Count one attempt for `key`. Returns False when this attempt goes over `limit` for the current window."""
    start = window_start(now or datetime.now(UTC), window)
    stmt = (
        insert(RateLimit)
        .values(key=key, window_start=start, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimit.key, RateLimit.window_start], set_={"count": RateLimit.count + 1}
        )
        .returning(RateLimit.count)
    )
    return int(session.execute(stmt).scalar_one()) <= limit


def purge(session: Session, older_than: timedelta, *, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - older_than
    return rowcount(session.execute(delete(RateLimit).where(RateLimit.window_start < cutoff)))
