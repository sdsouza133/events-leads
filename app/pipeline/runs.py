"""Small helpers for the run row, shared by the pipeline steps."""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, not_, select
from sqlalchemy.orm import Session

from app.models import Run, RunGrant, RunStatus

MIDNIGHT_SPREAD = timedelta(minutes=15)


def runs_used(session: Session, domain: str) -> int:
    """How many of a company's free runs are used: every run someone signed in for, except one that stopped before
    spending anything (a website we couldn't read, a run stopped because the company had used its runs), and one an
    operator started from the console."""
    stopped_free = and_(Run.status.in_([RunStatus.FAILED, RunStatus.CANCELED]), Run.spent_usd == 0)
    return (
        session.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.domain == domain,
                Run.status != RunStatus.AWAITING_SIGN_IN,
                not_(stopped_free),
                Run.operator_run.is_(False),
            )
        )
        or 0
    )


def has_run_left(session: Session, domain: str, free_runs: int) -> bool:
    """Whether the company may start another run: its free ones, plus one for each an operator granted it."""
    granted = session.scalar(select(func.count()).select_from(RunGrant).where(RunGrant.domain == domain)) or 0
    return runs_used(session, domain) < free_runs + granted


def lock_domain(session: Session, domain: str) -> None:
    """Hold the company's lock until the transaction ends, so two sign-ins can't both take its last free run."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"free-runs:{domain}"))))


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
