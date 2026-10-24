import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import budget
from app.budget import BudgetExceededError
from app.db import get_sessionmaker
from app.models import BudgetDay, Run, SpendEntry, SpendStatus
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)
CAP = Decimal("20.00")


def _day(db: Session) -> BudgetDay:
    row = db.get(BudgetDay, NOW.date(), populate_existing=True)
    assert row is not None
    return row


def test_reserve_holds_the_estimate_on_the_day_and_the_run(db: Session) -> None:
    run = make_run(db, budget_usd=Decimal("3.00"))
    spend_id = budget.reserve(
        db, provider="apify", estimated_usd=Decimal("1.25"), daily_cap_usd=CAP, run_id=run.id, now=NOW
    )
    db.commit()
    assert _day(db).reserved_usd == Decimal("1.25")
    db.refresh(run)
    assert run.reserved_usd == Decimal("1.25")
    entry = db.get(SpendEntry, spend_id)
    assert entry is not None
    assert (entry.status, entry.provider, entry.day) == (SpendStatus.RESERVED, "apify", NOW.date())


def test_the_daily_cap_stops_a_reservation(db: Session) -> None:
    budget.reserve(db, provider="apify", estimated_usd=Decimal("19.00"), daily_cap_usd=CAP, now=NOW)
    with pytest.raises(BudgetExceededError) as exc:
        budget.reserve(db, provider="apify", estimated_usd=Decimal("1.50"), daily_cap_usd=CAP, now=NOW)
    assert exc.value.scope == "day"
    assert exc.value.available == Decimal("1.00")


def test_the_run_budget_stops_a_reservation(db: Session) -> None:
    run = make_run(db, budget_usd=Decimal("3.00"))
    budget.reserve(db, provider="apify", estimated_usd=Decimal("2.50"), daily_cap_usd=CAP, run_id=run.id, now=NOW)
    with pytest.raises(BudgetExceededError) as exc:
        budget.reserve(db, provider="claude", estimated_usd=Decimal("0.75"), daily_cap_usd=CAP, run_id=run.id, now=NOW)
    assert exc.value.scope == "run"


def test_settle_swaps_the_estimate_for_the_actual_cost(db: Session) -> None:
    run = make_run(db)
    spend_id = budget.reserve(
        db, provider="apify", estimated_usd=Decimal("1.00"), daily_cap_usd=CAP, run_id=run.id, now=NOW
    )
    budget.settle(db, spend_id, Decimal("0.62"), units=Decimal(310), unit="post", now=NOW)
    db.commit()
    day = _day(db)
    assert (day.reserved_usd, day.spent_usd) == (Decimal(0), Decimal("0.62"))
    db.refresh(run)
    assert (run.reserved_usd, run.spent_usd) == (Decimal(0), Decimal("0.62"))
    entry = db.get(SpendEntry, spend_id)
    assert entry is not None
    assert (entry.status, entry.actual_usd, entry.unit) == (SpendStatus.SETTLED, Decimal("0.62"), "post")


def test_settling_twice_counts_once(db: Session) -> None:
    spend_id = budget.reserve(db, provider="apify", estimated_usd=Decimal("1.00"), daily_cap_usd=CAP, now=NOW)
    budget.settle(db, spend_id, Decimal("0.80"), now=NOW)
    budget.settle(db, spend_id, Decimal("0.80"), now=NOW)  # a retried job settling again
    db.commit()
    assert _day(db).spent_usd == Decimal("0.80")


def test_release_gives_the_reservation_back(db: Session) -> None:
    run = make_run(db)
    spend_id = budget.reserve(
        db, provider="apify", estimated_usd=Decimal("1.00"), daily_cap_usd=CAP, run_id=run.id, now=NOW
    )
    budget.release(db, spend_id, now=NOW)
    db.commit()
    assert (_day(db).reserved_usd, _day(db).spent_usd) == (Decimal(0), Decimal(0))
    entry = db.get(SpendEntry, spend_id)
    assert entry is not None
    assert entry.status == SpendStatus.RELEASED


def test_settling_after_midnight_adjusts_the_day_it_was_reserved_on(db: Session) -> None:
    spend_id = budget.reserve(db, provider="apify", estimated_usd=Decimal("1.00"), daily_cap_usd=CAP, now=NOW)
    budget.settle(db, spend_id, Decimal("1.00"), now=datetime(2026, 10, 24, 0, 30, tzinfo=UTC))
    db.commit()
    assert _day(db).spent_usd == Decimal("1.00")
    assert db.get(BudgetDay, NOW.date().replace(day=24)) is None


def test_negative_amounts_are_refused(db: Session) -> None:
    with pytest.raises(ValueError, match="negative"):
        budget.reserve(db, provider="apify", estimated_usd=Decimal("-1"), daily_cap_usd=CAP, now=NOW)
    spend_id = budget.reserve(db, provider="apify", estimated_usd=Decimal(1), daily_cap_usd=CAP, now=NOW)
    with pytest.raises(ValueError, match="negative"):
        budget.settle(db, spend_id, Decimal("-0.01"), now=NOW)


def test_unknown_run_or_reservation_is_an_error(db: Session) -> None:
    with pytest.raises(LookupError):
        budget.settle(db, 999_999, Decimal(1), now=NOW)


def test_remaining_today(db: Session) -> None:
    assert budget.remaining_today(db, CAP, now=NOW) == CAP
    budget.reserve(db, provider="apify", estimated_usd=Decimal("4.50"), daily_cap_usd=CAP, now=NOW)
    db.commit()
    assert budget.remaining_today(db, CAP, now=NOW) == Decimal("15.50")


def test_concurrent_workers_cannot_pass_the_daily_cap(db: Session) -> None:
    """Eight workers each try to reserve $3 under a $20 cap at the same moment: exactly six may succeed."""
    run_ids = []
    for i in range(8):
        run_ids.append(make_run(db, domain=f"co{i}.example", budget_usd=Decimal(10)).id)
    db.commit()

    start = threading.Barrier(len(run_ids))
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(run_id: uuid.UUID) -> None:
        session = get_sessionmaker()()
        try:
            start.wait()
            budget.reserve(
                session, provider="apify", estimated_usd=Decimal(3), daily_cap_usd=CAP, run_id=run_id, now=NOW
            )
            session.commit()
            result = "ok"
        except BudgetExceededError:
            session.rollback()
            result = "refused"
        finally:
            session.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in run_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) == ["ok"] * 6 + ["refused"] * 2
    assert _day(db).reserved_usd == Decimal(18)
    reserved_on_runs = db.scalars(select(Run.reserved_usd).where(Run.id.in_(run_ids))).all()
    assert sum(reserved_on_runs, Decimal(0)) == Decimal(18)


# ------------------------------------------------------------------------------------- recovery (ADR 0002)
def test_a_lower_cap_can_be_checked_than_the_whole_run_budget(db: Session) -> None:
    run = make_run(db, budget_usd=Decimal("3.00"))
    analysis = Decimal("0.50")
    budget.reserve(
        db, provider="claude", estimated_usd=Decimal("0.40"), daily_cap_usd=CAP, run_id=run.id, run_cap_usd=analysis
    )
    with pytest.raises(BudgetExceededError) as exc:
        budget.reserve(
            db, provider="claude", estimated_usd=Decimal("0.20"), daily_cap_usd=CAP, run_id=run.id, run_cap_usd=analysis
        )
    assert (exc.value.scope, exc.value.available) == ("run", Decimal("0.10"))


def _open(db: Session, *, started: bool, key: str = "r1:analyze_site", estimate: str = "0.15") -> int:
    spend_id = budget.reserve(
        db, provider="claude", estimated_usd=Decimal(estimate), daily_cap_usd=CAP, step_key=key, now=NOW
    )
    if started:
        budget.mark_started(db, spend_id, now=NOW)
    db.commit()
    return spend_id


def _entry(db: Session, spend_id: int) -> SpendEntry:
    entry = db.get(SpendEntry, spend_id, populate_existing=True)
    assert entry is not None
    return entry


def test_recover_releases_a_call_that_never_started_and_settles_one_that_did(db: Session) -> None:
    never = _open(db, started=False)
    maybe = _open(db, started=True)
    other_step = _open(db, started=True, key="r1:find_events")
    assert budget.recover(db, "r1:analyze_site", now=NOW) == 2
    db.commit()
    assert (_entry(db, never).status, _entry(db, never).actual_usd) == (SpendStatus.RELEASED, Decimal(0))
    assert (_entry(db, maybe).status, _entry(db, maybe).actual_usd) == (SpendStatus.SETTLED, Decimal("0.15"))
    assert _entry(db, other_step).status == SpendStatus.RESERVED  # another step's reservation is left alone
    assert _day(db).spent_usd == Decimal("0.15")
    assert budget.recover(db, "r1:analyze_site", now=NOW) == 0  # nothing left to close


def test_mark_started_keeps_the_first_time(db: Session) -> None:
    spend_id = _open(db, started=True)
    budget.mark_started(db, spend_id, now=NOW.replace(hour=23, minute=59))
    assert _entry(db, spend_id).started_at == NOW
    with pytest.raises(LookupError):
        budget.mark_started(db, 999_999)


def test_sweep_closes_only_reservations_open_too_long(db: Session) -> None:
    old_unstarted = _open(db, started=False, key="r1:a")
    old_started = _open(db, started=True, key="r1:b")
    later = datetime.now(UTC) + timedelta(hours=2)
    assert budget.sweep(db, timedelta(hours=3), now=later) == 0  # made under three hours before `later`
    assert budget.sweep(db, timedelta(hours=1), now=later) == 2
    db.commit()
    assert _entry(db, old_unstarted).status == SpendStatus.RELEASED
    assert _entry(db, old_started).status == SpendStatus.SETTLED
