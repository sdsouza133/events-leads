from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app import ratelimit

HOUR = timedelta(hours=1)
NOW = datetime(2026, 10, 23, 12, 20, tzinfo=UTC)


def test_windows_start_on_the_boundary() -> None:
    assert ratelimit.window_start(NOW, HOUR) == datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    assert ratelimit.window_start(NOW, timedelta(minutes=15)) == datetime(2026, 10, 23, 12, 15, tzinfo=UTC)


def test_a_zero_window_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one second"):
        ratelimit.window_start(NOW, timedelta(0))


@pytest.mark.db
def test_allows_up_to_the_limit_then_refuses(db: Session) -> None:
    results = [ratelimit.hit(db, "start:ip:203.0.113.9", limit=5, window=HOUR, now=NOW) for _ in range(7)]
    assert results == [True] * 5 + [False] * 2


@pytest.mark.db
def test_each_key_and_each_window_counts_separately(db: Session) -> None:
    for _ in range(5):
        ratelimit.hit(db, "start:ip:203.0.113.9", limit=5, window=HOUR, now=NOW)
    assert ratelimit.hit(db, "start:ip:198.51.100.4", limit=5, window=HOUR, now=NOW)
    assert ratelimit.hit(db, "start:ip:203.0.113.9", limit=5, window=HOUR, now=NOW + HOUR)


@pytest.mark.db
def test_old_windows_are_purged(db: Session) -> None:
    ratelimit.hit(db, "k", limit=5, window=HOUR, now=NOW - timedelta(days=2))
    ratelimit.hit(db, "k", limit=5, window=HOUR, now=NOW)
    assert ratelimit.purge(db, timedelta(days=1), now=NOW) == 1
