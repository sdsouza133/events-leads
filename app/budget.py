"""Spend caps that hold even with several workers.

Every paid call goes through two steps:

1. `reserve` holds the estimated cost against the day's cap and the run's budget, in one transaction that locks
   today's budget row (and the run), so two workers can't both slip under the cap. It returns a reservation id.
2. `mark_started`, committed right before the call, records that the call may have happened.
3. After the call, `settle` replaces the estimate with the actual cost, or `release` drops it if nothing was spent.
   Both are idempotent, so a job that crashes and runs again can't count the same spend twice.

A worker can stop anywhere in between. `recover` (for one step, when its job runs again) and `sweep` (for anything
left open too long) close what it left behind, by ADR 0002's rules: a reservation never marked started is released;
one that started but was never settled is settled at its full estimate, because the call may have been billed.

Caps apply to reservations. Providers that can overrun an estimate (token counts) may go over by that difference;
Apify runs are started with `maxTotalChargeUsd` equal to the reservation, so they can't.
Days are UTC days.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import BudgetDay, Run, SpendEntry, SpendStatus


class BudgetExceededError(Exception):
    def __init__(self, scope: Literal["day", "run"], needed: Decimal, available: Decimal) -> None:
        self.scope = scope
        self.needed = needed
        self.available = available
        super().__init__(f"{scope} budget: needs ${needed}, ${available} left")


def utc_day(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def _budget_day(session: Session, day: date) -> BudgetDay:
    session.execute(insert(BudgetDay).values(day=day).on_conflict_do_nothing(index_elements=[BudgetDay.day]))
    row = session.get(BudgetDay, day, with_for_update=True, populate_existing=True)
    if row is None:  # pragma: no cover - the insert above guarantees the row
        raise LookupError(day)
    return row


def _run(session: Session, run_id: uuid.UUID) -> Run:
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if run is None:
        raise LookupError(f"run {run_id} does not exist")
    return run


def reserve(
    session: Session,
    *,
    provider: str,
    estimated_usd: Decimal,
    daily_cap_usd: Decimal,
    run_id: uuid.UUID | None = None,
    run_cap_usd: Decimal | None = None,
    step_key: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> int:
    """Hold `estimated_usd` against today's cap and the run's budget. Raises BudgetExceededError if either is short.

    `run_cap_usd` checks the run against a lower cap than its whole budget, such as the analysis cap before approval.
    """
    if estimated_usd < 0:
        raise ValueError("estimated cost can't be negative")
    day = utc_day(now)
    # Lock order is always budget day, then run: the same order everywhere means no deadlocks.
    budget_day = _budget_day(session, day)
    run = _run(session, run_id) if run_id else None

    day_left = daily_cap_usd - budget_day.spent_usd - budget_day.reserved_usd
    if estimated_usd > day_left:
        raise BudgetExceededError("day", estimated_usd, max(day_left, Decimal(0)))
    if run is not None:
        cap = run.budget_usd if run_cap_usd is None else min(run_cap_usd, run.budget_usd)
        run_left = cap - run.spent_usd - run.reserved_usd
        if estimated_usd > run_left:
            raise BudgetExceededError("run", estimated_usd, max(run_left, Decimal(0)))
        run.reserved_usd += estimated_usd
    budget_day.reserved_usd += estimated_usd

    entry = SpendEntry(
        run_id=run_id, day=day, provider=provider, estimated_usd=estimated_usd, step_key=step_key, note=note
    )
    session.add(entry)
    session.flush()
    return entry.id


def mark_started(session: Session, spend_id: int, *, now: datetime | None = None) -> None:
    """The call is about to go out. Commit this before making it (ADR 0002)."""
    entry = session.get(SpendEntry, spend_id, with_for_update=True, populate_existing=True)
    if entry is None:
        raise LookupError(f"spend entry {spend_id} does not exist")
    if entry.started_at is None:
        entry.started_at = now or datetime.now(UTC)


def settle(
    session: Session,
    spend_id: int,
    actual_usd: Decimal,
    *,
    units: Decimal | None = None,
    unit: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record what the call really cost. Calling it again for the same reservation does nothing."""
    _close(session, spend_id, actual_usd, SpendStatus.SETTLED, units=units, unit=unit, now=now)


def settle_in_full(session: Session, spend_id: int, *, now: datetime | None = None) -> None:
    """The call's cost is unknown and it may have been billed: count the whole reservation (ADR 0002)."""
    entry = session.get(SpendEntry, spend_id)
    if entry is None:
        raise LookupError(f"spend entry {spend_id} does not exist")
    settle(session, spend_id, entry.estimated_usd, now=now)


def release(session: Session, spend_id: int, *, now: datetime | None = None) -> None:
    """The call didn't happen (or cost nothing): give the reservation back."""
    _close(session, spend_id, Decimal(0), SpendStatus.RELEASED, now=now)


def _close(
    session: Session,
    spend_id: int,
    actual_usd: Decimal,
    status: SpendStatus,
    *,
    units: Decimal | None = None,
    unit: str | None = None,
    now: datetime | None = None,
) -> None:
    if actual_usd < 0:
        raise ValueError("actual cost can't be negative")
    entry = session.get(SpendEntry, spend_id, with_for_update=True, populate_existing=True)
    if entry is None:
        raise LookupError(f"spend entry {spend_id} does not exist")
    if entry.status != SpendStatus.RESERVED:
        return  # already settled or released: a retried job must not count it twice

    budget_day = _budget_day(session, entry.day)
    budget_day.reserved_usd -= entry.estimated_usd
    budget_day.spent_usd += actual_usd
    if entry.run_id is not None:
        run = _run(session, entry.run_id)
        run.reserved_usd -= entry.estimated_usd
        run.spent_usd += actual_usd

    entry.status = status
    entry.actual_usd = actual_usd
    entry.units = units
    entry.unit = unit
    entry.settled_at = now or datetime.now(UTC)


def recover(session: Session, step_key: str, *, now: datetime | None = None) -> int:
    """Close what an earlier attempt at this step left open. Returns how many reservations it closed."""
    entries = session.scalars(
        select(SpendEntry)
        .where(SpendEntry.step_key == step_key, SpendEntry.status == SpendStatus.RESERVED)
        .order_by(SpendEntry.id)
        .with_for_update(skip_locked=True)
    ).all()
    for entry in entries:
        _close_abandoned(session, entry, now=now)
    return len(entries)


def sweep(session: Session, older_than: timedelta, *, now: datetime | None = None) -> int:
    """Close reservations still open `older_than` after they were made, so a run that died can't hold budget."""
    cutoff = (now or datetime.now(UTC)) - older_than
    entries = session.scalars(
        select(SpendEntry)
        .where(SpendEntry.status == SpendStatus.RESERVED, SpendEntry.created_at < cutoff)
        .order_by(SpendEntry.id)
        .limit(500)
        .with_for_update(skip_locked=True)  # a reservation being settled right now is left to its owner
    ).all()
    for entry in entries:
        _close_abandoned(session, entry, now=now)
    return len(entries)


def _close_abandoned(session: Session, entry: SpendEntry, *, now: datetime | None) -> None:
    if entry.started_at is None:
        release(session, entry.id, now=now)  # never started: nothing can have been charged
    else:
        settle(session, entry.id, entry.estimated_usd, now=now)  # may have been billed: count it in full


def remaining_today(session: Session, daily_cap_usd: Decimal, *, now: datetime | None = None) -> Decimal:
    row = session.get(BudgetDay, utc_day(now))
    if row is None:
        return daily_cap_usd
    return max(daily_cap_usd - row.spent_usd - row.reserved_usd, Decimal(0))
