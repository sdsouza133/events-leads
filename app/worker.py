"""The background worker (ADR 0002): claims jobs from the queue and runs them. Start it with `python -m app.worker`.

Each process runs one job at a time; run more processes for more throughput. While a job runs, a heartbeat thread
keeps its claim fresh, so `release_stale` hands a job to another worker only when its worker has really stopped.
Between jobs the worker does the housekeeping: it puts back jobs whose worker died and closes the spend reservations
such workers left open.

SIGTERM (a redeploy) and SIGINT stop the worker after the job it is running.
"""

import logging
import os
import random
import signal
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from types import TracebackType
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import budget, queue
from app.config import get_settings
from app.db import get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.models import JobStatus
from app.pipeline.base import Defer, GiveUp, JobContext, JobKind, Payload
from app.pipeline.jobs import JOBS
from app.pipeline.paid import SpendingPausedError
from app.providers.registry import build_providers
from app.providers.types import ProviderError

log = logging.getLogger(__name__)

MAINTENANCE_EVERY_SECONDS = 60.0
RESERVATIONS_OPEN_AT_MOST = timedelta(hours=1)
PAUSED_RECHECK = timedelta(minutes=5)


def worker_name() -> str:
    """Unique per process, as the queue requires: host, process and a random suffix (process ids get reused)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def describe(exc: BaseException) -> str:
    """A failure, for the job's last_error and the logs, without the personal data some messages quote: a validation
    error's names the input it rejected, so only where it failed is kept; a database error's detail names the values
    involved (which email already exists, say), so only its first line and SQLSTATE are kept."""
    if isinstance(exc, ValidationError):
        where = ", ".join(".".join(str(part) for part in error["loc"]) or "(root)" for error in exc.errors())
        return f"ValidationError: {exc.error_count()} problem(s) at {where}"
    if isinstance(exc, DBAPIError):
        primary = getattr(getattr(exc.orig, "diag", None), "message_primary", None)
        if primary:
            return f"{type(exc).__name__}: {primary} ({getattr(exc.orig, 'sqlstate', None)})"
        first_line = str(exc.orig).strip().splitlines()[:1]  # a client-side error: connection refused, say
        return f"{type(exc).__name__}: {first_line[0] if first_line else type(exc.orig).__name__}"
    return f"{type(exc).__name__}: {exc}"


class Worker:
    def __init__(self, ctx: JobContext, kinds: Mapping[str, JobKind], *, name: str | None = None) -> None:
        self.ctx = ctx
        self.kinds = dict(kinds)
        self.name = name or worker_name()
        self._stop = threading.Event()
        self._last_maintenance: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("worker started", extra={"worker": self.name, "kinds": sorted(self.kinds)})
        while not self._stop.is_set():
            try:
                self.maintain_if_due()
                worked = self.run_once()
            except SQLAlchemyError as exc:  # no traceback: a database error's can quote the values involved
                log.error("database unavailable, waiting before trying again", extra=self._failure(exc))
                worked = False
            except Exception:  # a bug outside any job (in housekeeping, say): keep the worker running
                log.exception("unexpected error in the worker loop", extra={"worker": self.name})
                worked = False
            if not worked:
                self._stop.wait(self.ctx.settings.worker_poll_seconds)
        log.info("worker stopped", extra={"worker": self.name})

    def run_once(self) -> bool:
        """Claim one job and run it. False when no job was due."""
        with session_scope() as session:
            job = queue.claim(session, self.name, kinds=list(self.kinds), now=self.ctx.now())
            if job is None:
                return False
            job_id, kind_name, payload = job.id, job.kind, dict(job.payload)
        context = {"job_id": job_id, "kind": kind_name, "run_id": payload.get("run_id"), "worker": self.name}
        log.info("job started", extra=context)
        started = time.monotonic()
        with _Heartbeat(job_id, self.name, self.ctx.settings.worker_heartbeat_seconds):
            self._run(job_id, self.kinds[kind_name], payload, context)
        log.info("job ended", extra={**context, "ms": round((time.monotonic() - started) * 1000)})
        return True

    def _run(self, job_id: int, kind: JobKind, payload: dict[str, Any], context: dict[str, Any]) -> None:
        session = get_sessionmaker()()
        try:
            outcome = kind.handle(self.ctx, session, payload)
            if isinstance(outcome, Defer):
                kept = queue.defer(session, job_id, self.name, outcome.delay, now=self.ctx.now())
            else:
                kept = queue.complete(session, job_id, self.name)
            if kept:
                session.commit()
            else:  # released as stale and claimed by another worker, which redoes the work
                session.rollback()
                log.warning("job was taken over by another worker; this attempt's writes were dropped", extra=context)
        except SpendingPausedError:  # PAUSE_NEW_RUNS: the job waits, without using up an attempt
            session.rollback()
            self._pause(job_id, context)
        except Exception as exc:  # any failure is recorded on the job, and the worker keeps going
            session.rollback()
            self._fail(job_id, kind, payload, exc, context)
        finally:
            session.close()

    def _pause(self, job_id: int, context: dict[str, Any]) -> None:
        with session_scope() as session:
            queue.defer(session, job_id, self.name, PAUSED_RECHECK, now=self.ctx.now())
        log.info("job paused: PAUSE_NEW_RUNS is set", extra=context)

    def _fail(
        self, job_id: int, kind: JobKind, payload: dict[str, Any], exc: Exception, context: dict[str, Any]
    ) -> None:
        retryable = exc.retryable if isinstance(exc, ProviderError) else True
        error = describe(exc)
        log.warning("job failed", extra={**context, "error": error, "retryable": retryable})
        if not isinstance(exc, ProviderError | ValidationError | SQLAlchemyError):  # our own bug: keep the traceback
            log.error("unexpected error in a job", exc_info=True, extra=context)
        with session_scope() as session:
            jitter = random.Random()  # noqa: S311 (spreads retries out; nothing secret)
            status = queue.fail(
                session, job_id, self.name, error, retryable=retryable, now=self.ctx.now(), jitter=jitter
            )
            if status == JobStatus.FAILED and kind.on_give_up is not None:
                self._give_up(kind.on_give_up, session, payload, context)

    def _give_up(self, hook: GiveUp, session: Session, payload: Payload, context: dict[str, Any]) -> None:
        """Run a give-up hook in a savepoint of its own: if it fails, the job is still recorded as failed, and the
        other jobs housekeeping is releasing still are."""
        try:
            with session.begin_nested():
                hook(self.ctx, session, payload)
        except Exception as exc:
            database = isinstance(exc, SQLAlchemyError)
            log.error("a give-up hook failed", exc_info=not database, extra={**context, "error": describe(exc)})

    def _failure(self, exc: BaseException) -> dict[str, Any]:
        return {"worker": self.name, "error": describe(exc)}

    def maintain_if_due(self) -> None:
        now = time.monotonic()
        if self._last_maintenance is not None and now - self._last_maintenance < MAINTENANCE_EVERY_SECONDS:
            return
        self._last_maintenance = now
        self.maintain()

    def maintain(self) -> None:
        """Put back jobs whose worker stopped answering, and close reservations nobody came back for."""
        stale_after = timedelta(seconds=self.ctx.settings.worker_stale_after_seconds)
        with session_scope() as session:
            released = queue.release_stale(session, stale_after, now=self.ctx.now())
            for job in released.given_up:
                kind = self.kinds.get(job.kind)
                if kind is not None and kind.on_give_up is not None:
                    context = {"job_id": job.id, "kind": job.kind, "worker": self.name}
                    self._give_up(kind.on_give_up, session, job.payload, context)
        with session_scope() as session:
            closed = budget.sweep(session, RESERVATIONS_OPEN_AT_MOST, now=self.ctx.now())
        if released.requeued or released.given_up or closed:
            log.info(
                "housekeeping",
                extra={
                    "requeued_jobs": released.requeued,
                    "given_up_jobs": len(released.given_up),
                    "closed_reservations": closed,
                },
            )


class _Heartbeat:
    """Keeps a running job's claim fresh from a background thread, on a connection of its own."""

    def __init__(self, job_id: int, worker: str, every_seconds: float) -> None:
        self._job_id = job_id
        self._worker = worker
        self._every = every_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{job_id}", daemon=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=self._every + 5)

    def _beat(self) -> None:
        while not self._stop.wait(self._every):
            try:
                with session_scope() as session:
                    alive = queue.heartbeat(session, self._job_id, self._worker)
            except SQLAlchemyError as exc:
                log.warning("heartbeat failed", extra={"job_id": self._job_id, "error": describe(exc)})
                continue
            if not alive:
                log.warning("job is no longer held by this worker", extra={"job_id": self._job_id})
                return


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker(JobContext(settings=settings, providers=build_providers(settings)), JOBS)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: worker.stop())
    worker.run_forever()


if __name__ == "__main__":
    main()
