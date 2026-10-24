"""A job queue in Postgres (ADR 0002).

The life of a job:

1. `enqueue` inserts it. With a `dedupe_key`, a second copy is ignored while the first is still pending.
2. A worker calls `claim` and commits straight away. `FOR UPDATE SKIP LOCKED` means two workers never get the same
   job; the job is now `running` and its attempt is counted.
3. The worker does the work, then calls `complete`, `fail` (retry later with backoff, or give up), or `defer` (not
   ready yet, try again later without using up an attempt), in the same transaction as the job's own writes.
4. If the worker dies, the job stays `running`; `release_stale` puts it back in the queue after a timeout. A worker
   on a long job calls `heartbeat` every so often, so the timeout can stay short without cutting off slow calls.

A worker that was only slow can come back after its job was released and claimed by another. So `complete`, `fail`
and `defer` change the job only while the calling worker still holds it, and report whether it did. When they report
False the caller must roll back: the job belongs to another worker now, which redoes the work. Worker names must be
unique per process.

Handlers must be idempotent: after a crash, the same job can run again.
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import rowcount
from app.models import Job, JobStatus

BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(hours=1)
MAX_ERROR_LENGTH = 4000

_PENDING = text("status IN ('queued', 'running')")  # must match the partial unique index on jobs.dedupe_key


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff(attempt: int, *, jitter: random.Random | None = None) -> timedelta:
    """Delay before retry number `attempt` (1-based): 30 s, 1 min, 2 min, ... capped at an hour.

    With `jitter`, the delay is a random point between half and all of that, so jobs that failed together
    (an API outage) don't all retry in the same second.
    """
    doublings = min(max(attempt - 1, 0), 16)  # 2**16 * 30 s is far past the cap already
    delay = min(BASE_BACKOFF * (1 << doublings), MAX_BACKOFF)
    if jitter is not None:
        delay *= jitter.uniform(0.5, 1.0)
    return delay


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Add a job. Returns its id, or None when a pending job with the same dedupe key already exists."""
    stmt = insert(Job).values(
        kind=kind,
        payload=payload or {},
        run_after=run_after or utcnow(),
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Job.dedupe_key], index_where=_PENDING)
    return session.execute(stmt.returning(Job.id)).scalar_one_or_none()


def claim(session: Session, worker: str, *, kinds: list[str] | None = None, now: datetime | None = None) -> Job | None:
    """Take the next due job, or None. Commit right after, so other workers see it as running."""
    now = now or utcnow()
    query = select(Job).where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
    if kinds:
        query = query.where(Job.kind.in_(kinds))
    job = session.scalars(query.order_by(Job.run_after, Job.id).limit(1).with_for_update(skip_locked=True)).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.locked_by = worker
    job.locked_at = now
    job.attempts += 1
    session.flush()
    return job


def _held(session: Session, job_id: int, worker: str) -> Job | None:
    """The job, row-locked, if `worker` still holds it; None if it was released as stale (and maybe re-claimed)."""
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise LookupError(f"job {job_id} does not exist")
    if job.status != JobStatus.RUNNING or job.locked_by != worker:
        return None
    return job


def complete(session: Session, job_id: int, worker: str) -> bool:
    """Mark the job done. False: `worker` no longer holds it (roll back)."""
    job = _held(session, job_id, worker)
    if job is None:
        return False
    job.status = JobStatus.DONE
    job.locked_by = None
    job.locked_at = None
    return True


def fail(
    session: Session,
    job_id: int,
    worker: str,
    error: str,
    *,
    retryable: bool = True,
    now: datetime | None = None,
    jitter: random.Random | None = None,
) -> JobStatus | None:
    """Record a failure. Retries with backoff while attempts remain, otherwise marks the job failed for good.

    Returns the job's new status, or None when `worker` no longer holds it (roll back).
    """
    now = now or utcnow()
    job = _held(session, job_id, worker)
    if job is None:
        return None
    job.last_error = error[:MAX_ERROR_LENGTH]
    job.locked_by = None
    job.locked_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.run_after = now + backoff(job.attempts, jitter=jitter)
    else:
        job.status = JobStatus.FAILED
    return job.status


def defer(session: Session, job_id: int, worker: str, delay: timedelta, *, now: datetime | None = None) -> bool:
    """Not ready yet (an Apify run still going, a manual AI answer not in): check again later, attempt not counted.

    False: `worker` no longer holds the job (roll back).
    """
    job = _held(session, job_id, worker)
    if job is None:
        return False
    job.status = JobStatus.QUEUED
    job.run_after = (now or utcnow()) + delay
    job.attempts = max(job.attempts - 1, 0)
    job.locked_by = None
    job.locked_at = None
    return True


def heartbeat(session: Session, job_id: int, worker: str, *, now: datetime | None = None) -> bool:
    """The job is still being worked on: restart its stale timer. False: `worker` no longer holds it."""
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING, Job.locked_by == worker)
        .values(locked_at=now or utcnow())
    )
    return rowcount(result) == 1


@dataclass(frozen=True)
class GivenUp:
    """A job `release_stale` failed for good: its worker stopped answering on its last attempt."""

    id: int
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Released:
    requeued: int
    given_up: list[GivenUp]


def release_stale(session: Session, older_than: timedelta, *, now: datetime | None = None) -> Released:
    """Put back jobs whose worker stopped answering. Jobs out of attempts are marked failed instead, and returned,
    so the caller can do what that kind of job does when it gives up (fail its run, for one)."""
    cutoff = (now or utcnow()) - older_than
    stale = (Job.status == JobStatus.RUNNING) & (Job.locked_at < cutoff)
    given_up = session.execute(
        update(Job)
        .where(stale, Job.attempts >= Job.max_attempts)
        .values(status=JobStatus.FAILED, locked_by=None, locked_at=None, last_error="worker stopped responding")
        .returning(Job.id, Job.kind, Job.payload)
    ).all()
    retry = session.execute(update(Job).where(stale).values(status=JobStatus.QUEUED, locked_by=None, locked_at=None))
    return Released(rowcount(retry), [GivenUp(job_id, kind, dict(payload)) for job_id, kind, payload in given_up])


def counts(session: Session) -> dict[str, int]:
    """Jobs per status, for the operator console."""
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {str(status): int(n) for status, n in rows}
