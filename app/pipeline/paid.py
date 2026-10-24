"""Paid calls that are safe to retry (ADR 0002, "Side effects outside the database").

A paid call is reserved, then marked started, each in a commit of its own, before it goes out. If it fails, the
failure says what it cost (`ProviderError.cost_usd`) and the reservation is settled or released to match. If it
succeeds, the caller settles the actual cost in the transaction that stores the answer, so the answer and its cost
are recorded together or not at all.

If the worker dies in between, the step's next attempt closes what it left open before reserving again: a
reservation that never started is released, and one that started is settled in full, because it may have been billed.
The worker's sweeper does the same for reservations nobody came back for.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app import budget
from app.db import session_scope
from app.pipeline.base import JobContext
from app.providers.types import Answer, AnswerPendingError, ProviderError

PAUSED_RECHECK = timedelta(minutes=5)


class SpendingPausedError(Exception):
    """Nothing billed may start now. The worker puts the job back, due again after `retry_after`, without using up
    an attempt."""

    def __init__(self, reason: str, retry_after: timedelta) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def check_not_paused(ctx: JobContext) -> None:
    """Call before anything billed: Claude and Apify (`paid_call` does), Apollo and Reoon.

    A job only sees the settings its worker started with, and changing PAUSE_NEW_RUNS restarts the worker. So a
    worker told to stop starts no paid call either: the job goes back at once, for the next worker to pick up.
    """
    if ctx.settings.pause_new_runs:
        raise SpendingPausedError("PAUSE_NEW_RUNS is set", PAUSED_RECHECK)
    if ctx.stopping():
        raise SpendingPausedError("the worker is stopping", timedelta(0))


@dataclass(frozen=True)
class Paid[T]:
    spend_id: int
    answer: Answer[T]


def step_key(run_id: uuid.UUID, step: str) -> str:
    return f"{run_id}:{step}"


def recover_step(ctx: JobContext, run_id: uuid.UUID, step: str) -> int:
    """Close the reservations an earlier attempt at this step left open. Returns how many there were."""
    with session_scope() as session:
        return budget.recover(session, step_key(run_id, step), now=ctx.now())


def paid_call[T](
    ctx: JobContext,
    *,
    run_id: uuid.UUID,
    step: str,
    provider: str,
    estimate: Decimal,
    call: Callable[[], Answer[T]],
    run_cap_usd: Decimal | None = None,
) -> Paid[T]:
    """Make one paid call under the rules above. Raises BudgetExceededError, before any call, when a cap is reached,
    and SpendingPausedError while PAUSE_NEW_RUNS is set.

    On success the caller must `budget.settle(session, paid.spend_id, paid.answer.cost_usd)` in the transaction that
    stores the answer.
    """
    check_not_paused(ctx)
    recover_step(ctx, run_id, step)
    with session_scope() as session:
        spend_id = budget.reserve(
            session,
            provider=provider,
            estimated_usd=estimate,
            daily_cap_usd=ctx.settings.daily_cap_usd,
            run_id=run_id,
            run_cap_usd=run_cap_usd,
            step_key=step_key(run_id, step),
            note=step,
            now=ctx.now(),
        )
    with session_scope() as session:
        budget.mark_started(session, spend_id, now=ctx.now())
    try:
        answer = call()
    except AnswerPendingError:  # manual AI mode: no one billable was asked
        with session_scope() as session:
            budget.release(session, spend_id, now=ctx.now())
        raise
    except ProviderError as exc:
        with session_scope() as session:
            if exc.cost_usd is None:  # it may have been billed: count it in full
                budget.settle(session, spend_id, estimate, now=ctx.now())
            elif exc.cost_usd > 0:
                budget.settle(session, spend_id, exc.cost_usd, now=ctx.now())
            else:
                budget.release(session, spend_id, now=ctx.now())
        raise
    except Exception:
        with session_scope() as session:
            budget.settle(session, spend_id, estimate, now=ctx.now())  # a bug of ours mid-call: outcome unknown
        raise
    return Paid(spend_id, answer)
