"""The paid-call protocol (ADR 0002): reserve, mark started, call, then settle or release by what happened."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import budget
from app.budget import BudgetExceededError
from app.config import Settings
from app.models import Run, SpendEntry, SpendStatus
from app.pipeline.base import JobContext
from app.pipeline.paid import SpendingPausedError, paid_call, step_key
from app.providers.registry import build_providers
from app.providers.types import Answer, ProviderError
from tests.factories import make_run

pytestmark = pytest.mark.db

ESTIMATE = Decimal("0.20")


def context(**settings: object) -> JobContext:
    configured = Settings.model_validate(settings)
    return JobContext(settings=configured, providers=build_providers(configured))


def spend(db: Session, run: Run) -> list[SpendEntry]:
    db.expire_all()
    return list(db.scalars(select(SpendEntry).where(SpendEntry.run_id == run.id).order_by(SpendEntry.id)))


def committed_run(db: Session) -> Run:
    run = make_run(db)
    db.commit()  # paid_call works in sessions of its own: they must see the run
    return run


def answer(cost: str) -> Answer[str]:
    return Answer("the answer", Decimal(cost))


def test_while_paused_nothing_is_reserved_or_called(db: Session) -> None:
    run = committed_run(db)
    called: list[str] = []

    def call() -> Answer[str]:
        called.append("called")
        return answer("0.07")

    with pytest.raises(SpendingPausedError):
        paid_call(
            context(pause_new_runs=True),
            run_id=run.id,
            step="analyze_site",
            provider="claude",
            estimate=ESTIMATE,
            call=call,
        )
    assert called == []
    assert spend(db, run) == []


def test_a_successful_call_is_started_and_left_for_the_caller_to_settle(db: Session) -> None:
    run = committed_run(db)
    seen: list[SpendStatus] = []

    def call() -> Answer[str]:
        [entry] = spend(db, run)
        assert entry.started_at is not None  # committed before the call went out
        seen.append(entry.status)
        return answer("0.07")

    paid = paid_call(context(), run_id=run.id, step="analyze_site", provider="claude", estimate=ESTIMATE, call=call)
    assert seen == [SpendStatus.RESERVED]
    assert paid.answer.value == "the answer"
    [entry] = spend(db, run)
    assert (entry.status, entry.estimated_usd, entry.step_key) == (
        SpendStatus.RESERVED,
        ESTIMATE,
        step_key(run.id, "analyze_site"),
    )
    budget.settle(db, paid.spend_id, paid.answer.cost_usd)
    db.commit()
    assert spend(db, run)[0].actual_usd == Decimal("0.07")


@pytest.mark.parametrize(
    ("cost", "status", "actual"),
    [
        (Decimal("0.03"), SpendStatus.SETTLED, Decimal("0.03")),  # answered and billed, then failed
        (Decimal(0), SpendStatus.RELEASED, Decimal(0)),  # an error answer: nothing billed
        (None, SpendStatus.SETTLED, ESTIMATE),  # unknown: counted in full
    ],
)
def test_a_failed_call_is_closed_by_what_it_cost(
    db: Session, cost: Decimal | None, status: SpendStatus, actual: Decimal
) -> None:
    run = committed_run(db)

    def call() -> Answer[str]:
        raise ProviderError("no luck", retryable=True, cost_usd=cost)

    with pytest.raises(ProviderError):
        paid_call(context(), run_id=run.id, step="analyze_site", provider="claude", estimate=ESTIMATE, call=call)
    [entry] = spend(db, run)
    assert (entry.status, entry.actual_usd) == (status, actual)


def test_a_bug_during_the_call_counts_the_call_in_full(db: Session) -> None:
    run = committed_run(db)

    def call() -> Answer[str]:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        paid_call(context(), run_id=run.id, step="analyze_site", provider="claude", estimate=ESTIMATE, call=call)
    [entry] = spend(db, run)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, ESTIMATE)


def test_what_a_crashed_attempt_left_open_is_closed_before_reserving_again(db: Session) -> None:
    run = committed_run(db)
    ctx = context()
    key = step_key(run.id, "analyze_site")
    crashed = budget.reserve(
        db,
        provider="claude",
        estimated_usd=ESTIMATE,
        daily_cap_usd=ctx.settings.daily_cap_usd,
        run_id=run.id,
        step_key=key,
    )
    budget.mark_started(db, crashed)  # the call went out, then the worker died
    db.commit()

    paid = paid_call(
        ctx, run_id=run.id, step="analyze_site", provider="claude", estimate=ESTIMATE, call=lambda: answer("0.05")
    )
    first, second = spend(db, run)
    assert (first.id, first.status, first.actual_usd) == (crashed, SpendStatus.SETTLED, ESTIMATE)
    assert (second.id, second.status) == (paid.spend_id, SpendStatus.RESERVED)
    db.refresh(run)
    assert (run.spent_usd, run.reserved_usd) == (ESTIMATE, ESTIMATE)


def test_no_call_is_made_when_a_cap_is_reached(db: Session) -> None:
    run = committed_run(db)
    calls: list[str] = []

    def call() -> Answer[str]:
        calls.append("called")
        return answer("0")

    with pytest.raises(BudgetExceededError):
        paid_call(
            context(daily_cap_usd="0.10"),
            run_id=run.id,
            step="analyze_site",
            provider="claude",
            estimate=ESTIMATE,
            call=call,
        )
    assert calls == []
    assert spend(db, run) == []
