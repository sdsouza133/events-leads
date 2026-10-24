"""What a job handler is given and what it hands back to the worker.

A handler gets an open session for its final writes. The worker commits them together with the job's completion,
so they happen once or not at all. Two rules keep that true (ADR 0002):

1. Never hold a transaction open while waiting on an outside service: read what the step needs, commit, then call.
   Production enforces this, since Postgres ends any session left idle inside a transaction for 60 seconds.
2. Paid calls go through `app.pipeline.paid.paid_call`, which records each reservation in commits of its own.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.providers.registry import Providers


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobContext:
    settings: Settings
    providers: Providers
    clock: Callable[[], datetime] = utcnow

    def now(self) -> datetime:
        return self.clock()

    def today(self) -> date:
        """Today in UTC: the budget day, and the date event windows are measured from."""
        return self.now().astimezone(UTC).date()


@dataclass(frozen=True)
class Done:
    """The job is finished. The worker marks it done in the same transaction as the handler's writes."""


@dataclass(frozen=True)
class Defer:
    """Not ready yet (a search still running, the day's budget used up): run again after `delay`, without using up
    an attempt."""

    delay: timedelta


DONE = Done()

Outcome = Done | Defer
Payload = dict[str, Any]
Handler = Callable[[JobContext, Session, Payload], Outcome]
GiveUp = Callable[[JobContext, Session, Payload], None]


@dataclass(frozen=True)
class JobKind:
    handle: Handler
    on_give_up: GiveUp | None = None
    """Called, in the transaction that fails the job for good, once no attempt is left (to fail its run, say)."""
