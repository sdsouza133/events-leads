"""The worker loop: how a handler's outcome or failure becomes the job's state, and the housekeeping between jobs."""

import dataclasses
import signal
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import budget, queue, worker
from app.config import Settings
from app.db import session_scope
from app.models import Account, Job, JobStatus, Run, SpendEntry, SpendStatus
from app.pipeline.base import DONE, Defer, JobContext, JobKind, Outcome, Payload
from app.pipeline.paid import PAUSED_RECHECK, check_not_paused
from app.providers.registry import build_providers
from app.providers.types import ProviderError
from app.worker import Worker, describe
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def context(now: datetime = NOW) -> JobContext:
    settings = Settings()
    return JobContext(settings=settings, providers=build_providers(settings), clock=lambda: now)


def job(db: Session, job_id: int) -> Job:
    found = db.get(Job, job_id, populate_existing=True)
    assert found is not None
    return found


def one_job(db: Session, *, run: Run | None = None, max_attempts: int = 5) -> int:
    payload = {"run_id": str(run.id)} if run else {}
    job_id = queue.enqueue(db, "test", payload, run_after=NOW, max_attempts=max_attempts)
    assert job_id is not None
    db.commit()
    return job_id


def make_worker(handle: Callable[[JobContext, Session, Payload], Outcome], **kind: Any) -> Worker:
    return Worker(context(), {"test": JobKind(handle, **kind)}, name="worker-a")


def write_note(detail: str) -> Callable[[JobContext, Session, Payload], None]:
    def write(ctx: JobContext, session: Session, payload: Payload) -> None:
        run = session.get(Run, uuid.UUID(payload["run_id"]))
        assert run is not None
        run.status_detail = detail

    return write


def note_on_run(detail: str) -> Callable[[JobContext, Session, Payload], Outcome]:
    write = write_note(detail)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        write(ctx, session, payload)
        return DONE

    return handle


def test_nothing_due_means_nothing_to_do(db: Session) -> None:
    assert make_worker(note_on_run("x")).run_once() is False


def test_the_handlers_writes_commit_with_the_completion(db: Session) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run)
    assert make_worker(note_on_run("written")).run_once() is True
    assert job(db, job_id).status == JobStatus.DONE
    db.refresh(run)
    assert run.status_detail == "written"


def test_defer_requeues_the_job_without_using_an_attempt(db: Session) -> None:
    job_id = one_job(db)
    make_worker(lambda _ctx, _session, _payload: Defer(timedelta(minutes=5))).run_once()
    deferred = job(db, job_id)
    assert (deferred.status, deferred.attempts, deferred.run_after) == (JobStatus.QUEUED, 0, NOW + timedelta(minutes=5))


def test_a_job_held_by_the_pause_waits_without_using_an_attempt(db: Session) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        note_on_run("half done")(ctx, session, payload)
        check_not_paused(ctx)
        return DONE

    w = Worker(dataclasses.replace(context(), settings=Settings(pause_new_runs=True)), {"test": JobKind(handle)})
    w.run_once()
    paused = job(db, job_id)
    assert (paused.status, paused.attempts, paused.run_after, paused.last_error) == (
        JobStatus.QUEUED,
        0,
        NOW + PAUSED_RECHECK,
        None,
    )
    db.refresh(run)
    assert run.status_detail is None  # what it wrote before stopping was rolled back


def test_a_worker_told_to_stop_starts_no_paid_call_and_hands_the_job_on(db: Session) -> None:
    job_id = one_job(db)
    called: list[str] = []

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        w.stop()  # SIGTERM arrives while the job runs: a redeploy, or the worker scaled down
        check_not_paused(ctx)
        called.append("a paid call")
        return DONE

    w = make_worker(handle)
    w.run_once()
    assert called == []
    handed_on = job(db, job_id)
    assert (handed_on.status, handed_on.attempts, handed_on.run_after) == (JobStatus.QUEUED, 0, NOW)  # due at once


def test_a_failure_rolls_back_and_is_retried_later(db: Session) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        note_on_run("half done")(ctx, session, payload)
        raise ProviderError("Apollo is busy (503)", retryable=True)

    make_worker(handle).run_once()
    failed = job(db, job_id)
    assert (failed.status, failed.attempts, failed.last_error) == (
        JobStatus.QUEUED,
        1,
        "ProviderError: Apollo is busy (503)",
    )
    assert failed.run_after > NOW
    db.refresh(run)
    assert run.status_detail is None  # the failed attempt's writes were rolled back


@pytest.mark.parametrize(
    ("error", "max_attempts"),
    [
        (ProviderError("rejected our credentials (401)", retryable=False), 5),  # not worth retrying
        (RuntimeError("a bug"), 1),  # the last attempt
    ],
)
def test_a_job_that_gives_up_calls_its_give_up_hook(db: Session, error: Exception, max_attempts: int) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run, max_attempts=max_attempts)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        raise error

    make_worker(handle, on_give_up=write_note("gave up")).run_once()
    assert job(db, job_id).status == JobStatus.FAILED
    db.refresh(run)
    assert run.status_detail == "gave up"


class _Email(BaseModel):
    email: int


def test_validation_errors_are_recorded_without_the_input_they_rejected(db: Session) -> None:
    job_id = one_job(db)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        _Email.model_validate({"email": "jane.doe@acme.example"})
        return DONE

    make_worker(handle).run_once()
    error = job(db, job_id).last_error
    assert error == "ValidationError: 1 problem(s) at email"
    assert "jane.doe" not in error


def test_describe_names_the_failure() -> None:
    assert describe(KeyError("run_id")) == "KeyError: 'run_id'"


def test_a_worker_that_lost_its_job_drops_its_writes(db: Session) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        with session_scope() as other:  # meanwhile: released as stale and claimed by worker-b
            queue.release_stale(other, timedelta(seconds=1), now=NOW + timedelta(hours=1))
        with session_scope() as other:
            queue.claim(other, "worker-b", now=NOW + timedelta(hours=1))
        return note_on_run("written by worker-a")(ctx, session, payload)

    make_worker(handle).run_once()
    taken = job(db, job_id)
    assert (taken.status, taken.locked_by) == (JobStatus.RUNNING, "worker-b")
    db.refresh(run)
    assert run.status_detail is None


def test_housekeeping_requeues_stale_jobs_and_gives_up_on_exhausted_ones(db: Session) -> None:
    run = make_run(db)
    retry_id = one_job(db)
    last_try_id = one_job(db, run=run, max_attempts=1)
    for _ in range(2):
        queue.claim(db, "dead-worker", now=NOW)
    db.commit()

    later = NOW + timedelta(seconds=Settings().worker_stale_after_seconds + 1)
    w = Worker(context(now=later), {"test": JobKind(note_on_run("x"), on_give_up=write_note("worker died"))})
    w.maintain()
    assert job(db, retry_id).status == JobStatus.QUEUED
    assert job(db, last_try_id).status == JobStatus.FAILED
    db.refresh(run)
    assert run.status_detail == "worker died"


def broken_hook(ctx: JobContext, session: Session, payload: Payload) -> None:
    raise KeyError("run_id")  # e.g. a payload the hook didn't expect


def test_a_failing_give_up_hook_still_records_the_job_as_failed(db: Session) -> None:
    job_id = one_job(db, max_attempts=1)

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        raise RuntimeError("a bug")

    make_worker(handle, on_give_up=broken_hook).run_once()
    failed = job(db, job_id)
    assert (failed.status, failed.last_error) == (JobStatus.FAILED, "RuntimeError: a bug")


def test_one_failing_give_up_hook_does_not_hold_back_the_other_stale_jobs(db: Session) -> None:
    run = make_run(db)
    broken_id = queue.enqueue(db, "broken", run_after=NOW, max_attempts=1)
    fine_id = one_job(db, run=run, max_attempts=1)
    for _ in range(2):
        queue.claim(db, "dead-worker", now=NOW)
    db.commit()

    later = NOW + timedelta(seconds=Settings().worker_stale_after_seconds + 1)
    kinds = {
        "broken": JobKind(note_on_run("x"), on_give_up=broken_hook),
        "test": JobKind(note_on_run("x"), on_give_up=write_note("worker died")),
    }
    Worker(context(now=later), kinds).maintain()
    assert broken_id is not None
    assert job(db, broken_id).status == job(db, fine_id).status == JobStatus.FAILED
    db.refresh(run)
    assert run.status_detail == "worker died"


def test_an_unexpected_error_between_jobs_does_not_stop_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    w = Worker(context(), {})
    rounds: list[str] = []

    def maintain_if_due() -> None:
        rounds.append("housekeeping")
        if len(rounds) == 1:
            raise RuntimeError("a bug in housekeeping")
        w.stop()

    monkeypatch.setattr(w, "maintain_if_due", maintain_if_due)
    monkeypatch.setattr(w, "run_once", lambda: True)
    w.run_forever()
    assert rounds == ["housekeeping", "housekeeping"]


def test_database_errors_are_described_without_the_values_involved(db: Session) -> None:
    email = "jane.doe@acme.example"
    db.add(Account(email=email, domain="acme.example"))
    db.commit()
    db.add(Account(email=email, domain="acme.example"))
    with pytest.raises(IntegrityError) as caught:
        db.commit()
    db.rollback()
    described = describe(caught.value)
    assert described.startswith("IntegrityError: duplicate key value violates unique constraint")
    assert described.endswith("(23505)")
    assert "jane.doe" not in described
    assert "jane.doe" not in str(caught.value).split("DETAIL")[0]  # bound parameters are hidden too


def test_a_database_error_from_the_client_side_is_described_by_its_first_line() -> None:
    error = OperationalError("SELECT 1", {}, Exception("connection refused\n\tIs the server running?"))
    assert describe(error) == "OperationalError: connection refused"


def test_housekeeping_closes_reservations_left_open(db: Session) -> None:
    spend_id = budget.reserve(db, provider="claude", estimated_usd=Decimal("0.15"), daily_cap_usd=Decimal(20))
    budget.mark_started(db, spend_id)
    db.commit()
    Worker(context(now=datetime.now(UTC) + timedelta(hours=2)), {}).maintain()
    entry = db.get(SpendEntry, spend_id, populate_existing=True)
    assert entry is not None
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, Decimal("0.15"))


def test_maintenance_runs_at_most_once_a_minute(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Worker(context(), {})
    calls: list[str] = []
    monkeypatch.setattr(w, "maintain", lambda: calls.append("maintained"))
    w.maintain_if_due()
    w.maintain_if_due()
    assert calls == ["maintained"]


def test_the_heartbeat_keeps_the_claim_fresh(db: Session) -> None:
    claimed_at = datetime.now(UTC) - timedelta(minutes=10)
    job_id = queue.enqueue(db, "test", run_after=claimed_at)
    assert job_id is not None
    db.commit()
    assert queue.claim(db, "worker-a", now=claimed_at) is not None
    db.commit()
    with worker._Heartbeat(job_id, "worker-a", every_seconds=0.05):
        deadline = time.monotonic() + 5  # wait for a beat instead of guessing when the thread gets to run
        while (job(db, job_id).locked_at or claimed_at) <= claimed_at + timedelta(minutes=9):
            assert time.monotonic() < deadline, "no heartbeat within 5 seconds"
            time.sleep(0.05)
    locked_at = job(db, job_id).locked_at
    assert locked_at is not None
    assert locked_at > claimed_at + timedelta(minutes=9)


def test_the_heartbeat_stops_once_the_job_belongs_to_someone_else(db: Session) -> None:
    job_id = one_job(db)
    queue.claim(db, "worker-b", now=NOW)
    db.commit()
    beat = worker._Heartbeat(job_id, "worker-a", every_seconds=0.05)
    with beat:
        beat._thread.join(timeout=5)  # it ends on its own once it finds the job gone
        assert not beat._thread.is_alive()
    assert job(db, job_id).locked_at == NOW


def test_run_forever_works_until_stopped(db: Session) -> None:
    run = make_run(db)
    job_id = one_job(db, run=run)
    w = make_worker(note_on_run("x"))

    def handle(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
        w.stop()  # e.g. SIGTERM arrives while the job runs: it still finishes
        return note_on_run("finished")(ctx, session, payload)

    w.kinds["test"] = JobKind(handle)
    w.run_forever()
    assert job(db, job_id).status == JobStatus.DONE


def test_main_starts_a_worker_that_stops_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Any] = {}
    started: list[Worker] = []

    def record(self: Worker) -> None:  # instead of looping forever
        started.append(self)

    monkeypatch.setattr(signal, "signal", handlers.__setitem__)
    monkeypatch.setattr(Worker, "run_forever", record)
    worker.main()
    [w] = started
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert w._stop.is_set()
    assert "analyze_site" in w.kinds


def test_jobs_of_other_kinds_are_left_for_other_workers(db: Session) -> None:
    other = queue.enqueue(db, "someone_elses", run_after=NOW)
    assert other is not None
    db.commit()
    assert make_worker(note_on_run("x")).run_once() is False
    assert db.scalars(select(Job.status).where(Job.id == other)).one() == JobStatus.QUEUED


def test_a_database_outage_does_not_stop_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    w = Worker(context(), {})
    attempts: list[str] = []

    def run_once() -> bool:
        attempts.append("tried")
        if len(attempts) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        w.stop()
        return True

    monkeypatch.setattr(w, "maintain_if_due", lambda: None)
    monkeypatch.setattr(w, "run_once", run_once)
    w.ctx = dataclasses.replace(w.ctx, settings=Settings(worker_poll_seconds=0.01))
    w.run_forever()
    assert attempts == ["tried", "tried"]
