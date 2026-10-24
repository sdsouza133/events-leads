import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app import queue
from app.db import get_sessionmaker
from app.models import Job, JobStatus

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ pure functions
@pytest.mark.parametrize(
    ("attempt", "seconds"),
    [(1, 30), (2, 60), (3, 120), (4, 240), (8, 3600), (50, 3600)],
)
def test_backoff_doubles_and_caps_at_an_hour(attempt: int, seconds: int) -> None:
    assert queue.backoff(attempt) == timedelta(seconds=seconds)


def test_backoff_jitter_stays_between_half_and_full_delay() -> None:
    rng = random.Random(7)
    for attempt in range(1, 10):
        full = queue.backoff(attempt)
        assert full / 2 <= queue.backoff(attempt, jitter=rng) <= full


# -------------------------------------------------------------------------------------------------- with Postgres
needs_db = pytest.mark.db


@needs_db
def test_enqueue_then_claim_marks_the_job_running(db: Session) -> None:
    job_id = queue.enqueue(db, "analyze", {"run_id": "r1"}, run_after=NOW)
    db.commit()
    job = queue.claim(db, "worker-a", now=NOW)
    assert job is not None
    assert job.id == job_id
    assert (job.status, job.locked_by, job.attempts) == (JobStatus.RUNNING, "worker-a", 1)
    assert job.payload == {"run_id": "r1"}


@needs_db
def test_jobs_are_not_claimed_before_they_are_due(db: Session) -> None:
    queue.enqueue(db, "poll", run_after=NOW + timedelta(minutes=1))
    db.commit()
    assert queue.claim(db, "w", now=NOW) is None
    assert queue.claim(db, "w", now=NOW + timedelta(minutes=1)) is not None


@needs_db
def test_oldest_due_job_comes_first_and_kinds_can_be_filtered(db: Session) -> None:
    late = queue.enqueue(db, "collect", run_after=NOW - timedelta(minutes=1))
    early = queue.enqueue(db, "analyze", run_after=NOW - timedelta(minutes=5))
    db.commit()
    first = queue.claim(db, "w", kinds=["collect"], now=NOW)
    assert first is not None
    assert first.id == late
    second = queue.claim(db, "w", now=NOW)
    assert second is not None
    assert second.id == early


@needs_db
def test_two_workers_never_get_the_same_job(db: Session) -> None:
    first_id = queue.enqueue(db, "analyze", run_after=NOW)
    second_id = queue.enqueue(db, "analyze", run_after=NOW)
    db.commit()

    other: Session = get_sessionmaker()()
    try:
        held = queue.claim(db, "worker-a", now=NOW)  # row lock held: this transaction stays open
        taken = queue.claim(other, "worker-b", now=NOW)  # must skip the locked row, not wait for it
        assert held is not None
        assert taken is not None
        assert {held.id, taken.id} == {first_id, second_id}
        other.commit()
    finally:
        other.close()


@needs_db
def test_dedupe_key_ignores_a_second_pending_copy(db: Session) -> None:
    first = queue.enqueue(db, "deliver", dedupe_key="deliver:r1")
    duplicate = queue.enqueue(db, "deliver", dedupe_key="deliver:r1")
    assert first is not None
    assert duplicate is None


@needs_db
def test_dedupe_key_is_free_again_once_the_job_is_done(db: Session) -> None:
    first = queue.enqueue(db, "deliver", dedupe_key="deliver:r1", run_after=NOW)
    db.commit()
    job = queue.claim(db, "w", now=NOW)
    assert job is not None
    assert queue.complete(db, job.id, "w")
    db.commit()
    again = queue.enqueue(db, "deliver", dedupe_key="deliver:r1")
    assert again is not None
    assert again != first


@needs_db
def test_failures_retry_with_backoff_then_give_up(db: Session) -> None:
    job_id = queue.enqueue(db, "enrich", max_attempts=2, run_after=NOW)
    assert job_id is not None
    db.commit()

    queue.claim(db, "w", now=NOW)
    assert queue.fail(db, job_id, "w", "Apollo 503", now=NOW) == JobStatus.QUEUED
    job = db.get(Job, job_id)
    assert job is not None
    assert job.run_after == NOW + timedelta(seconds=30)
    assert job.last_error == "Apollo 503"

    queue.claim(db, "w", now=NOW + timedelta(seconds=30))
    assert queue.fail(db, job_id, "w", "Apollo 503 again", now=NOW) == JobStatus.FAILED


@needs_db
def test_non_retryable_failure_gives_up_at_once(db: Session) -> None:
    job_id = queue.enqueue(db, "enrich", run_after=NOW)
    assert job_id is not None
    db.commit()
    queue.claim(db, "w", now=NOW)
    assert queue.fail(db, job_id, "w", "bad input", retryable=False, now=NOW) == JobStatus.FAILED


@needs_db
def test_long_errors_are_truncated(db: Session) -> None:
    job_id = queue.enqueue(db, "enrich", run_after=NOW)
    assert job_id is not None
    db.commit()
    queue.claim(db, "w", now=NOW)
    queue.fail(db, job_id, "w", "x" * 10_000, now=NOW)
    job = db.get(Job, job_id)
    assert job is not None
    assert job.last_error is not None
    assert len(job.last_error) == queue.MAX_ERROR_LENGTH


@needs_db
def test_defer_reschedules_without_using_an_attempt(db: Session) -> None:
    job_id = queue.enqueue(db, "poll_apify", max_attempts=1, run_after=NOW)
    assert job_id is not None
    db.commit()
    for minute in range(5):  # polling five times must not exhaust a single attempt
        job = queue.claim(db, "w", now=NOW + timedelta(minutes=minute))
        assert job is not None
        assert queue.defer(db, job_id, "w", timedelta(minutes=1), now=NOW + timedelta(minutes=minute))
    job = db.get(Job, job_id)
    assert job is not None
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


@needs_db
def test_jobs_of_a_dead_worker_are_released(db: Session) -> None:
    retry_id = queue.enqueue(db, "collect", run_after=NOW)
    last_try_id = queue.enqueue(db, "collect", max_attempts=1, run_after=NOW)
    db.commit()
    queue.claim(db, "dead-worker", now=NOW)
    queue.claim(db, "dead-worker", now=NOW)
    db.commit()

    early = queue.release_stale(db, timedelta(minutes=15), now=NOW + timedelta(minutes=5))
    assert (early.requeued, early.given_up) == (0, [])
    late = queue.release_stale(db, timedelta(minutes=15), now=NOW + timedelta(minutes=20))
    assert late.requeued == 1
    assert [(g.id, g.kind) for g in late.given_up] == [(last_try_id, "collect")]
    retried, gave_up = db.get(Job, retry_id), db.get(Job, last_try_id)
    assert retried is not None
    assert gave_up is not None
    db.refresh(retried)
    db.refresh(gave_up)
    assert (retried.status, retried.locked_by) == (JobStatus.QUEUED, None)
    assert gave_up.status == JobStatus.FAILED


@needs_db
def test_counts_by_status(db: Session) -> None:
    queue.enqueue(db, "a", run_after=NOW)
    queue.enqueue(db, "b", run_after=NOW)
    db.commit()
    job = queue.claim(db, "w", now=NOW)
    assert job is not None
    assert queue.counts(db) == {"queued": 1, "running": 1}


@needs_db
def test_completing_an_unknown_job_is_an_error(db: Session) -> None:
    with pytest.raises(LookupError):
        queue.complete(db, 123456, "w")


@needs_db
def test_a_worker_that_lost_its_job_cannot_change_it(db: Session) -> None:
    """Worker A is slow, its job is released and claimed by B: A's late complete, fail or defer must not touch it."""
    job_id = queue.enqueue(db, "collect", run_after=NOW)
    assert job_id is not None
    db.commit()
    queue.claim(db, "worker-a", now=NOW)
    db.commit()
    queue.release_stale(db, timedelta(minutes=15), now=NOW + timedelta(minutes=20))
    db.commit()
    reclaimed = queue.claim(db, "worker-b", now=NOW + timedelta(minutes=20))
    assert reclaimed is not None
    db.commit()

    assert queue.complete(db, job_id, "worker-a") is False
    assert queue.fail(db, job_id, "worker-a", "late failure", now=NOW) is None
    assert queue.defer(db, job_id, "worker-a", timedelta(minutes=1), now=NOW) is False
    db.commit()

    job = db.get(Job, job_id)
    assert job is not None
    db.refresh(job)
    assert (job.status, job.locked_by, job.attempts, job.last_error) == (JobStatus.RUNNING, "worker-b", 2, None)
    assert queue.complete(db, job_id, "worker-b")


@needs_db
def test_a_finished_job_cannot_be_finished_again(db: Session) -> None:
    job_id = queue.enqueue(db, "deliver", run_after=NOW)
    assert job_id is not None
    db.commit()
    queue.claim(db, "w", now=NOW)
    assert queue.complete(db, job_id, "w")
    assert queue.fail(db, job_id, "w", "after the fact", now=NOW) is None
    job = db.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.DONE


@needs_db
def test_a_heartbeat_keeps_a_long_job_from_being_released(db: Session) -> None:
    job_id = queue.enqueue(db, "find_events", run_after=NOW)
    assert job_id is not None
    db.commit()
    queue.claim(db, "worker-a", now=NOW)
    db.commit()
    assert queue.heartbeat(db, job_id, "worker-a", now=NOW + timedelta(minutes=10))
    db.commit()
    released = queue.release_stale(db, timedelta(minutes=15), now=NOW + timedelta(minutes=20))
    assert released.requeued == 0  # it beat 10 minutes ago, inside the 15-minute window
    assert not queue.heartbeat(db, job_id, "worker-b", now=NOW + timedelta(minutes=21))  # not worker-b's job
