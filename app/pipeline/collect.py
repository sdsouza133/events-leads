"""Collect: the public LinkedIn posts about each approved event (issue #5).

The requester's approval (`approve`) stores the events and queues one `collect_event` job per event. Each job:

1. Leaves out the searches any run made for the same event in the last SEARCH_CACHE_DAYS: posts are shared, so a
   popular event gets cheaper with every company that asks about it. While another run is searching the event, it
   waits for that search instead of paying for the same posts.
2. Starts one Apify run for the rest, capped at the amount reserved for it (`maxTotalChargeUsd`), and stores the run's
   id before waiting on it, so a worker that stops mid-search picks the same run back up instead of paying again.
3. Polls it, deferring in between (which uses no attempt), then stores the posts, which search found each and the
   searches themselves, and settles the reservation at what Apify charged.

A search that fails or runs too long is tried once more; after that only its event is marked failed. The run moves on
to qualify once no event is left to search, and fails only if none could be searched. When the day's cap is used up,
the run waits for the next UTC day.

Locks are taken in one order, so jobs can't deadlock: the event (FOR NO KEY UPDATE, which still lets other runs link
posts to it), then the budget (spend entry, budget day), then the run, then the run's event.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import budget, queue
from app.budget import BudgetExceededError
from app.models import Event, EventPost, Post, Run, RunEvent, RunEventStatus, RunStatus, SearchCache
from app.pipeline import runs
from app.pipeline.base import DONE, Defer, JobContext, Outcome, Payload
from app.pipeline.paid import paid_call, step_key
from app.providers.pricing import apify_search_estimate
from app.providers.types import Answer, ProviderError, SearchedPost, SearchRun, SearchStatus
from app.schemas import MAX_QUERIES, ApprovedEvent

COLLECT_EVENT = "collect_event"
QUALIFY = "qualify"  # the next step's job (issue #6)
MAX_EVENTS = 3
SEARCH_TRIES = 2  # a search that fails or runs too long is tried once more
SEARCH_GIVE_UP_AFTER = timedelta(minutes=30)  # Apify stops a run at 15 minutes; this is for one that never says so
MAX_POST_ID_CHARS = 64
ACTIVE = (RunStatus.COLLECTING, RunStatus.WAITING_FOR_BUDGET)
OPEN = (RunEventStatus.PENDING, RunEventStatus.SEARCHING)

SEARCHING = "Searching LinkedIn for posts about your events"
WAITING = "We've reached today's capacity. Your search will continue automatically after midnight UTC."
READING_POSTS = "Reading the posts to find who was there"
NOTHING_COLLECTED = "We couldn't search LinkedIn for your events. We've been notified."


def approve(session: Session, run_id: uuid.UUID, events: list[ApprovedEvent], *, now: datetime) -> bool:
    """Store the events the requester approved and start collecting, in the caller's transaction.

    Returns False, changing nothing, when the run isn't awaiting approval (a second click, say).
    """
    if not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError(f"a run searches 1 to {MAX_EVENTS} events")
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.AWAITING_APPROVAL:
        return False
    for approved, queries in _merged(events):
        event_id = _event_id(session, approved)
        session.add(RunEvent(run_id=run_id, event_id=event_id, source=approved.source, queries=queries))
        queue.enqueue(
            session,
            COLLECT_EVENT,
            {"run_id": str(run_id), "event_id": str(event_id)},
            run_after=now,
            dedupe_key=f"{COLLECT_EVENT}:{run_id}:{event_id}",
        )
    run.status = RunStatus.COLLECTING
    run.status_detail = SEARCHING
    run.approved_at = now
    return True


def event_key(name: str, start: date | None) -> str:
    """The name's words plus its year, so "SaaStr Annual" in 2026 and "saastr annual 2026" are one event."""
    words = re.findall(r"\w+", name.casefold())
    if start is not None and str(start.year) not in words:
        words.append(str(start.year))
    return "-".join(words)[:200] or hashlib.sha256(name.encode()).hexdigest()[:16]


def collect_event(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    run_id, event_id = uuid.UUID(payload["run_id"]), uuid.UUID(payload["event_id"])
    run = session.get(Run, run_id)
    run_event = session.get(RunEvent, (run_id, event_id))
    if run is None or run_event is None or run_event.status not in OPEN:
        return DONE  # gone, or finished by an earlier attempt
    search = _Search.of(run_event)
    if search is None and run.status not in ACTIVE:  # canceled or failed: start nothing new
        run_event.status = RunEventStatus.PENDING  # (and don't make other runs wait on a search that never started)
        return DONE
    session.commit()  # no transaction stays open while Apify answers
    if search is None:
        return _start(ctx, session, run_id, event_id)
    return _poll(ctx, session, run_id, event_id, search)


def give_up(ctx: JobContext, session: Session, payload: Payload) -> None:
    """The job failed for good: close its search's reservation and go on without this event."""
    run_id, event_id = uuid.UUID(payload["run_id"]), uuid.UUID(payload["event_id"])
    budget.recover(session, step_key(run_id, _step(event_id)), now=ctx.now())
    runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run_event is None or run_event.status not in OPEN:
        return
    run_event.status = RunEventStatus.FAILED
    run_event.error = "the collect job failed after its last attempt"
    _move_on_if_collected(ctx, session, run_id)


@dataclass(frozen=True)
class _Search:
    """The Apify run an event is waiting on."""

    apify_run_id: str
    spend_id: int
    queries: list[str]
    started_at: datetime

    @classmethod
    def of(cls, run_event: RunEvent) -> "_Search | None":
        if run_event.apify_run_id is None or run_event.spend_id is None or run_event.search_started_at is None:
            return None
        return cls(
            run_event.apify_run_id, run_event.spend_id, list(run_event.search_queries), run_event.search_started_at
        )


def _start(ctx: JobContext, session: Session, run_id: uuid.UUID, event_id: uuid.UUID) -> Outcome:
    now = ctx.now()
    # Deciding what to search holds the event, so two runs approving it at once don't both pay for its posts.
    session.get(Event, event_id, with_for_update={"key_share": True})
    runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run_event is None or run_event.status not in OPEN:
        return DONE
    fresh_since = now - timedelta(days=ctx.settings.search_cache_days)
    cached = set(
        session.scalars(
            select(SearchCache.query).where(SearchCache.event_id == event_id, SearchCache.fetched_at > fresh_since)
        )
    )
    todo = [query for query in run_event.queries if query not in cached]
    if not todo:
        run_event.status = RunEventStatus.DONE
        run_event.collected_at = now
        _move_on_if_collected(ctx, session, run_id)
        return DONE
    if _searched_by_another_run(session, run_id, event_id, now):
        return Defer(_poll_delay(ctx))  # when it finishes, its searches are in the cache
    run_event.status = RunEventStatus.SEARCHING  # what other runs wait on
    run_event.search_queries = todo
    run_event.search_started_at = now
    run_event.search_attempts += 1
    session.commit()

    per_query = ctx.settings.search_posts_per_query
    estimate = apify_search_estimate(len(todo), per_query)
    try:
        paid = paid_call(
            ctx,
            run_id=run_id,
            step=_step(event_id),
            provider="apify",
            estimate=estimate,
            call=lambda: Answer(
                ctx.providers.search.start(todo, max_posts_per_query=per_query, max_charge_usd=estimate)
            ),
        )
    except BudgetExceededError as exc:
        return _not_started(ctx, session, run_id, event_id, exc)
    except ProviderError as exc:
        if exc.retryable:
            _not_started(ctx, session, run_id, event_id)
            session.commit()  # kept: the worker rolls back the rest when the job fails
            raise
        return _search_failed(ctx, session, run_id, event_id, reason=f"the search couldn't start: {exc}")

    # Store the run's id right away: from here on, every attempt polls this run instead of paying again.
    run = runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run_event is not None:
        run_event.apify_run_id = paid.answer.value.id
        run_event.spend_id = paid.spend_id
    if run is not None and run.status == RunStatus.WAITING_FOR_BUDGET:
        run.status = RunStatus.COLLECTING
        run.status_detail = SEARCHING
    session.commit()
    return Defer(_poll_delay(ctx))


def _poll(ctx: JobContext, session: Session, run_id: uuid.UUID, event_id: uuid.UUID, search: _Search) -> Outcome:
    try:
        found = ctx.providers.search.get(search.apify_run_id)
    except ProviderError as exc:
        if exc.retryable:
            raise
        reason = f"the search couldn't be checked: {exc}"
        return _search_failed(ctx, session, run_id, event_id, reason=reason, search=search)
    if found.status == SearchStatus.RUNNING:
        if ctx.now() - search.started_at > SEARCH_GIVE_UP_AFTER:
            return _search_failed(ctx, session, run_id, event_id, reason="the search ran too long", search=search)
        return Defer(_poll_delay(ctx))
    if found.status == SearchStatus.FAILED:
        reason = f"the search failed: {found.message or 'no reason given'}"
        return _search_failed(ctx, session, run_id, event_id, reason=reason, search=search, cost=found.cost_usd)
    try:
        posts = ctx.providers.search.posts(found)
    except ProviderError as exc:
        if exc.retryable:
            raise  # the next attempt polls again and refetches them
        reason = f"the search's posts couldn't be read: {exc}"
        return _search_failed(ctx, session, run_id, event_id, reason=reason, search=search, cost=found.cost_usd)
    return _store(ctx, session, run_id, event_id, search=search, found=found, posts=posts)


def _store(
    ctx: JobContext,
    session: Session,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    search: _Search,
    found: SearchRun,
    posts: list[SearchedPost],
) -> Outcome:
    now = ctx.now()
    if found.cost_usd is None:
        budget.settle_in_full(session, search.spend_id, now=now)
    else:
        budget.settle(session, search.spend_id, found.cost_usd, units=Decimal(len(posts)), unit="post", now=now)
    _save_posts(session, event_id, posts, now=now)
    per_query = ((found.cost_usd or Decimal(0)) / len(search.queries)).quantize(Decimal("0.0001"))
    for query in sorted(search.queries):  # rows are always locked in the same order
        stmt = insert(SearchCache).values(
            event_id=event_id,
            query=query,
            post_count=sum(1 for post in posts if post.query == query),
            cost_usd=per_query,
            fetched_at=now,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[SearchCache.event_id, SearchCache.query],
                set_={"post_count": stmt.excluded.post_count, "cost_usd": per_query, "fetched_at": now},
            )
        )
    runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run_event is not None:
        run_event.status = RunEventStatus.DONE
        run_event.collected_at = now
        run_event.error = None
    _move_on_if_collected(ctx, session, run_id)
    return DONE


def _save_posts(session: Session, event_id: uuid.UUID, posts: list[SearchedPost], *, now: datetime) -> None:
    first: dict[str, SearchedPost] = {}
    for post in posts:
        if len(post.id) <= MAX_POST_ID_CHARS:
            first.setdefault(post.id, post)  # a post several searches found is stored once, under the first
    if not first:
        return
    rows = [
        {
            "id": post.id,
            "url": post.url[:2048],
            "author_name": post.author_name[:300] if post.author_name else None,
            "author_url": post.author_url[:2048] if post.author_url else None,
            "author_headline": post.author_headline,
            "author_kind": post.author_kind[:16],
            "text": post.text,
            "posted_at": post.posted_at,
            "fetched_at": now,
        }
        for post in sorted(first.values(), key=lambda post: post.id)  # locked in the same order by every run
    ]
    stmt = insert(Post).values(rows)
    newer = ("url", "author_name", "author_url", "author_headline", "author_kind", "text", "posted_at", "fetched_at")
    session.execute(stmt.on_conflict_do_update(index_elements=[Post.id], set_={c: stmt.excluded[c] for c in newer}))
    links = [{"event_id": event_id, "post_id": post.id, "query": (post.query or "")[:500]} for post in first.values()]
    session.execute(insert(EventPost).values(links).on_conflict_do_nothing())


def _search_failed(
    ctx: JobContext,
    session: Session,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    reason: str,
    search: _Search | None = None,
    cost: Decimal | None = None,
) -> Outcome:
    """Settle what the search cost (in full if unknown), then try once more or go on without the event."""
    if search is not None:
        if cost is None:
            budget.settle_in_full(session, search.spend_id, now=ctx.now())
        else:
            budget.settle(session, search.spend_id, cost, now=ctx.now())
    runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run_event is None:
        return DONE
    run_event.apify_run_id = None
    run_event.spend_id = None
    run_event.error = reason
    if run_event.search_attempts < SEARCH_TRIES:
        run_event.status = RunEventStatus.PENDING
        return Defer(_poll_delay(ctx))
    run_event.status = RunEventStatus.FAILED
    _move_on_if_collected(ctx, session, run_id)
    return DONE


def _not_started(
    ctx: JobContext,
    session: Session,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    exc: BudgetExceededError | None = None,
) -> Outcome:
    """The search was never started (no budget, or Apify couldn't take it): undo the attempt."""
    run = runs.locked(session, run_id)
    run_event = _locked(session, run_id, event_id)
    if run is None or run_event is None:
        return DONE
    run_event.status = RunEventStatus.PENDING
    run_event.search_attempts -= 1
    run_event.search_queries = []
    run_event.search_started_at = None
    if exc is None:
        return DONE  # the caller re-raises: the worker retries the job later
    if exc.scope == "run":
        run_event.status = RunEventStatus.FAILED
        run_event.error = f"the run's budget can't cover this search ({exc})"
        _move_on_if_collected(ctx, session, run_id)
        return DONE
    if run.status == RunStatus.COLLECTING:
        run.status = RunStatus.WAITING_FOR_BUDGET
        run.status_detail = WAITING
    return Defer(runs.until_tomorrow(ctx.now()))


def _move_on_if_collected(ctx: JobContext, session: Session, run_id: uuid.UUID) -> None:
    """Once no event is left to search, hand the run to qualify, or fail it if no event could be searched."""
    run = runs.locked(session, run_id)
    if run is None or run.status not in ACTIVE:
        return
    statuses = set(session.scalars(select(RunEvent.status).where(RunEvent.run_id == run_id)))
    if statuses & set(OPEN):
        return
    if RunEventStatus.DONE not in statuses:
        runs.fail(run, NOTHING_COLLECTED, now=ctx.now(), error="no event could be searched")
        return
    run.status = RunStatus.QUALIFYING
    run.status_detail = READING_POSTS
    queue.enqueue(session, QUALIFY, {"run_id": str(run_id)}, run_after=ctx.now(), dedupe_key=f"{QUALIFY}:{run_id}")


def _searched_by_another_run(session: Session, run_id: uuid.UUID, event_id: uuid.UUID, now: datetime) -> bool:
    other = session.scalar(
        select(RunEvent.run_id)
        .where(
            RunEvent.event_id == event_id,
            RunEvent.run_id != run_id,
            RunEvent.status == RunEventStatus.SEARCHING,
            RunEvent.search_started_at > now - SEARCH_GIVE_UP_AFTER,
        )
        .limit(1)
    )
    return other is not None


def _merged(events: list[ApprovedEvent]) -> list[tuple[ApprovedEvent, list[str]]]:
    """One entry per event: the same event approved twice is searched once, with both lists of queries."""
    by_key: dict[str, tuple[ApprovedEvent, list[str]]] = {}
    for approved in events:
        _, queries = by_key.setdefault(event_key(approved.name, approved.start_date), (approved, []))
        for query in approved.queries:
            if len(queries) < MAX_QUERIES and query.casefold() not in (q.casefold() for q in queries):
                queries.append(query)
    return list(by_key.values())


def _event_id(session: Session, approved: ApprovedEvent) -> uuid.UUID:
    """The shared event's id, creating it the first time any run approves it."""
    key = event_key(approved.name, approved.start_date)
    session.execute(
        insert(Event)
        .values(
            id=uuid.uuid4(),
            key=key,
            name=approved.name,
            short_name=approved.short_name,
            hashtags=approved.hashtags,
            start_date=approved.start_date,
            end_date=approved.end_date,
            location=approved.location,
            url=approved.url,
        )
        .on_conflict_do_nothing(index_elements=[Event.key])
    )
    return session.scalars(select(Event.id).where(Event.key == key)).one()


def _locked(session: Session, run_id: uuid.UUID, event_id: uuid.UUID) -> RunEvent | None:
    """The run's event, row-locked and fresh. Lock the run first (see the module docstring)."""
    return session.get(RunEvent, (run_id, event_id), with_for_update=True, populate_existing=True)


def _step(event_id: uuid.UUID) -> str:
    return f"collect:{event_id}"


def _poll_delay(ctx: JobContext) -> timedelta:
    return timedelta(seconds=ctx.settings.search_poll_seconds)
