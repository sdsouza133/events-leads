"""Small helpers for the run row, shared by the pipeline steps."""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Run, RunStatus

MIDNIGHT_SPREAD = timedelta(minutes=15)


def locked(session: Session, run_id: uuid.UUID) -> Run | None:
    """The run, row-locked for the rest of the transaction, as it is now (not as an earlier read left it).

    In a transaction that also settles or reserves spend, call this after the budget call: the budget locks the
    budget day before the run, and every transaction must take the two in that order, or two can deadlock.
    """
    return session.get(Run, run_id, with_for_update=True, populate_existing=True)


def fail(run: Run, detail: str, *, now: datetime, error: str | None = None) -> None:
    """Stop the run. `detail` is shown to the requester; `error` is for operators only."""
    run.status = RunStatus.FAILED
    run.status_detail = detail
    run.error = error
    run.completed_at = now


def until_tomorrow(now: datetime, *, rng: random.Random | None = None) -> timedelta:
    """How long until the next UTC day's budget opens, plus up to 15 minutes, so runs waiting on the daily cap don't
    all start in the same second."""
    utc = now.astimezone(UTC)
    midnight = datetime(utc.year, utc.month, utc.day, tzinfo=UTC) + timedelta(days=1)
    spread = (rng or random.Random()).uniform(0, MIDNIGHT_SPREAD.total_seconds())  # noqa: S311 (not security)
    return midnight - utc + timedelta(seconds=spread)
