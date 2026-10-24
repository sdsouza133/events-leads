"""Collecting posts for the approved events (issue #5): one search per event, reused for days, safe to rerun."""

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import budget
from app.config import Settings
from app.db import session_scope
from app.models import (
    Event,
    EventPost,
    Job,
    JobStatus,
    Post,
    Run,
    RunEvent,
    RunEventStatus,
    RunStatus,
    SearchCache,
    SpendEntry,
    SpendStatus,
)
from app.pipeline import collect
from app.pipeline.base import DONE, JobContext
from app.pipeline.collect import COLLECT_EVENT, QUALIFY, approve, event_key
from app.pipeline.jobs import JOBS
from app.pipeline.paid import step_key
from app.providers.fakes import FakePostSearch
from app.providers.registry import build_providers
from app.providers.types import ProviderError, SearchedPost, SearchRun, SearchStatus
from app.schemas import ApprovedEvent
from app.worker import Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
QUERIES = ['"Pipeline Summit 2026"', "#PipelineSummit"]


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class StubSearch(FakePostSearch):
    """The fake search, with the failures a test needs, recording the searches it was asked to start."""

    failures: int = 0  # how many searches fail before one succeeds
    never_finishes: bool = False
    start_error: ProviderError | None = None
    started: list[list[str]] = field(default_factory=list)

    def start(self, queries: list[str], *, max_posts_per_query: int, max_charge_usd: Decimal) -> SearchRun:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(list(queries))
        return super().start(queries, max_posts_per_query=max_posts_per_query, max_charge_usd=max_charge_usd)

    def get(self, run_id: str) -> SearchRun:
        if self.never_finishes:
            return SearchRun(id=run_id, status=SearchStatus.RUNNING)
        if len(self.started) <= self.failures:
            return SearchRun(id=run_id, status=SearchStatus.FAILED, cost_usd=Decimal("0.01"), message="actor crashed")
        return super().get(run_id)


def worker(clock: Clock, search: FakePostSearch | None = None, **settings: object) -> Worker:
    configured = Settings.model_validate(settings)
    providers = build_providers(configured)
    providers = dataclasses.replace(providers, search=search or StubSearch())
    return Worker(JobContext(settings=configured, providers=providers, clock=clock), JOBS, name="worker-a")


def event(name: str = "Pipeline Summit", queries: list[str] | None = None, **fields: object) -> ApprovedEvent:
    return ApprovedEvent.model_validate({"name": name, "queries": queries or QUERIES, **fields})


def approved_run(db: Session, *events: ApprovedEvent, budget_usd: str = "3.00", when: datetime = NOW) -> Run:
    """A run whose requester just approved the plan, as the plan page leaves it."""
    run = make_run(db, status=RunStatus.AWAITING_APPROVAL, budget_usd=Decimal(budget_usd))
    assert approve(db, run.id, list(events) or [event()], now=when)
    db.commit()
    return run


def run_jobs(db: Session, w: Worker, clock: Clock, *, max_steps: int = 30) -> None:
    """Run the worker until no collect job is waiting, moving the clock to each deferred job's time."""
    for _ in range(max_steps):
        if w.run_once():
            continue
        db.expire_all()
        due = db.scalar(
            select(func.min(Job.run_after)).where(Job.kind == COLLECT_EVENT, Job.status == JobStatus.QUEUED)
        )
        if due is None:
            return
        clock.now = max(clock.now, due)
    raise AssertionError("collect jobs are still waiting")


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found = db.get(Run, run.id)
    assert found is not None
    return found


def run_events(db: Session, run: Run) -> list[RunEvent]:
    db.expire_all()
    return list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))


def spend(db: Session, run: Run) -> list[SpendEntry]:
    db.expire_all()
    return list(db.scalars(select(SpendEntry).where(SpendEntry.run_id == run.id).order_by(SpendEntry.id)))


def jobs(db: Session, kind: str) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == kind).order_by(Job.id)))


# ----------------------------------------------------------------------------------------------------- approval
def test_approval_stores_the_events_and_queues_one_search_per_event(db: Session) -> None:
    run = approved_run(db, event(), event("RevOps Forum", ['"RevOps Forum"'], source="requester"))

    done = fresh(db, run)
    assert (done.status, done.status_detail, done.approved_at) == (RunStatus.COLLECTING, collect.SEARCHING, NOW)
    assert sorted(db.scalars(select(Event.key))) == ["pipeline-summit", "revops-forum"]
    assert sorted((e.source, e.status, tuple(e.queries)) for e in run_events(db, run)) == [
        ("requester", RunEventStatus.PENDING, ('"RevOps Forum"',)),
        ("suggested", RunEventStatus.PENDING, tuple(QUERIES)),
    ]
    assert len(jobs(db, COLLECT_EVENT)) == 2
    assert approve(db, run.id, [event()], now=NOW) is False  # a second click changes nothing


@pytest.mark.parametrize("count", [0, collect.MAX_EVENTS + 1])
def test_a_run_searches_one_to_three_events(db: Session, count: int) -> None:
    run = make_run(db, status=RunStatus.AWAITING_APPROVAL)
    with pytest.raises(ValueError, match="1 to 3 events"):
        approve(db, run.id, [event(f"Event {i}") for i in range(count)], now=NOW)


def test_the_same_event_approved_twice_is_searched_once_with_both_sets_of_queries(db: Session) -> None:
    run = approved_run(
        db, event(queries=QUERIES), event("pipeline SUMMIT", ['"Pipeline Summit" AND recap', QUERIES[0]])
    )
    [one] = run_events(db, run)
    assert one.queries == [*QUERIES, '"Pipeline Summit" AND recap']


def test_events_already_known_are_shared_between_runs(db: Session) -> None:
    approved_run(db, event())
    approved_run(db, event())
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_event_keys_ignore_case_punctuation_and_a_repeated_year() -> None:
    start = datetime(2026, 9, 9, tzinfo=UTC).date()
    assert event_key("SaaStr Annual", start) == "saastr-annual-2026"
    assert event_key("saastr annual 2026", None) == "saastr-annual-2026"
    assert event_key("SaaStr Annual — 2026!", start) == "saastr-annual-2026"
    assert event_key("!!!", None) == event_key("!!!", None) != ""


# ---------------------------------------------------------------------------------------------------- searching
def test_a_search_runs_and_its_posts_are_stored(db: Session) -> None:
    clock, search = Clock(), StubSearch(posts_per_query=9)
    run = approved_run(db)
    run_jobs(db, worker(clock, search), clock)

    assert search.started == [QUERIES]
    [done] = run_events(db, run)
    assert (done.status, done.search_attempts, done.collected_at) == (
        RunEventStatus.DONE,
        1,
        NOW + timedelta(seconds=20),
    )
    assert db.scalar(select(func.count()).select_from(Post)) == 18
    links = db.execute(select(EventPost.query, func.count()).group_by(EventPost.query))
    assert sorted(tuple(row) for row in links) == sorted((q, 9) for q in QUERIES)
    cached = sorted((c.query, c.post_count, c.cost_usd) for c in db.scalars(select(SearchCache)))
    assert cached == sorted((q, 9, Decimal("0.018")) for q in QUERIES)  # (sorted here: collations order quotes apart)

    [entry] = spend(db, run)
    assert (entry.provider, entry.status, entry.estimated_usd, entry.actual_usd, entry.units) == (
        "apify",
        SpendStatus.SETTLED,
        Decimal("0.20"),  # 2 searches x 50 posts x $0.002: the most Apify may charge
        Decimal("0.036"),
        Decimal(18),
    )
    moved = fresh(db, run)
    assert (moved.status, moved.status_detail) == (RunStatus.QUALIFYING, collect.READING_POSTS)
    [qualify] = jobs(db, QUALIFY)
    assert qualify.payload == {"run_id": str(run.id)}


def test_the_run_moves_on_only_when_every_event_is_collected(db: Session) -> None:
    clock = Clock()
    w = worker(clock)
    run = approved_run(db, event(), event("RevOps Forum", ['"RevOps Forum"']))
    w.run_once()  # starts the first event's search
    w.run_once()  # starts the second's
    clock.now += timedelta(seconds=20)
    w.run_once()  # the first is stored
    assert fresh(db, run).status == RunStatus.COLLECTING
    w.run_once()
    assert fresh(db, run).status == RunStatus.QUALIFYING


def test_a_recent_search_of_the_same_event_is_reused_at_no_cost(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    first = approved_run(db)
    run_jobs(db, worker(clock, search), clock)

    clock.now += timedelta(days=2)
    second = approved_run(db, when=clock.now)
    run_jobs(db, worker(clock, search), clock)
    assert search.started == [QUERIES]  # no second search
    assert spend(db, second) == []
    assert [e.status for e in run_events(db, second)] == [RunEventStatus.DONE]
    assert fresh(db, second).status == RunStatus.QUALIFYING
    assert len(spend(db, first)) == 1


def test_only_the_searches_not_already_cached_are_run(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    approved_run(db, event(queries=[QUERIES[0]]))
    run_jobs(db, worker(clock, search), clock)

    second = approved_run(db, event(queries=QUERIES), when=clock.now)
    run_jobs(db, worker(clock, search), clock)
    assert search.started == [[QUERIES[0]], [QUERIES[1]]]
    [done] = run_events(db, second)
    assert done.search_queries == [QUERIES[1]]


def test_a_search_older_than_the_cache_window_is_run_again(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    approved_run(db)
    run_jobs(db, worker(clock, search), clock)

    clock.now += timedelta(days=3, minutes=1)
    approved_run(db, when=clock.now)
    run_jobs(db, worker(clock, search), clock)
    assert search.started == [QUERIES, QUERIES]
    assert {c.fetched_at for c in db.scalars(select(SearchCache))} == {clock.now}


def test_a_run_waits_for_another_runs_search_of_the_same_event(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    w = worker(clock, search)
    first = approved_run(db)
    w.run_once()  # the first run's search starts
    second = approved_run(db)
    w.run_once()  # the second run waits for it instead of paying for the same posts
    [waiting] = run_events(db, second)
    assert (waiting.status, waiting.search_attempts) == (RunEventStatus.PENDING, 0)
    assert spend(db, second) == []

    run_jobs(db, w, clock)
    assert search.started == [QUERIES]
    assert fresh(db, first).status == fresh(db, second).status == RunStatus.QUALIFYING
    assert spend(db, second) == []


# --------------------------------------------------------------------------------------------------- failures
def test_a_failed_search_is_tried_once_more(db: Session) -> None:
    clock, search = Clock(), StubSearch(failures=1)
    run = approved_run(db)
    run_jobs(db, worker(clock, search), clock)

    assert len(search.started) == 2
    [done] = run_events(db, run)
    assert (done.status, done.search_attempts, done.error) == (RunEventStatus.DONE, 2, None)
    failed, succeeded = spend(db, run)
    assert (failed.status, failed.actual_usd) == (SpendStatus.SETTLED, Decimal("0.01"))  # what the failed run charged
    assert succeeded.status == SpendStatus.SETTLED
    assert fresh(db, run).status == RunStatus.QUALIFYING


def test_an_event_whose_search_fails_twice_is_skipped(db: Session) -> None:
    clock = Clock()
    search = StubSearch(failing_queries={'"RevOps Forum"'})
    run = approved_run(db, event(), event("RevOps Forum", ['"RevOps Forum"']))
    run_jobs(db, worker(clock, search), clock)

    by_name = {e.queries[0]: e for e in run_events(db, run)}
    skipped = by_name['"RevOps Forum"']
    assert (skipped.status, skipped.search_attempts) == (RunEventStatus.FAILED, 2)
    assert skipped.error == "the search failed: fake failure"
    assert by_name[QUERIES[0]].status == RunEventStatus.DONE
    assert fresh(db, run).status == RunStatus.QUALIFYING  # one event is enough to go on


def test_a_run_whose_every_search_fails_fails(db: Session) -> None:
    clock = Clock()
    run = approved_run(db)
    run_jobs(db, worker(clock, StubSearch(failures=2)), clock)
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, collect.NOTHING_COLLECTED)
    assert jobs(db, QUALIFY) == []
    assert all(entry.status == SpendStatus.SETTLED for entry in spend(db, run))


def test_a_search_that_never_finishes_is_counted_in_full_and_tried_again(db: Session) -> None:
    clock, search = Clock(), StubSearch(never_finishes=True)
    run = approved_run(db)
    w = worker(clock, search)
    w.run_once()  # started
    clock.now += timedelta(minutes=29)
    w.run_once()  # still running: wait
    assert run_events(db, run)[0].status == RunEventStatus.SEARCHING
    clock.now += timedelta(minutes=2)
    w.run_once()  # 31 minutes: given up
    [entry] = spend(db, run)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, entry.estimated_usd)  # may have been billed
    [retry] = run_events(db, run)
    assert (retry.status, retry.error) == (RunEventStatus.PENDING, "the search ran too long")


def test_a_search_apify_refuses_to_start_is_tried_once_more_then_skipped(db: Session) -> None:
    clock = Clock()
    search = StubSearch(start_error=ProviderError("Apify refused the request (400)", retryable=False))
    run = approved_run(db)
    run_jobs(db, worker(clock, search), clock)
    [skipped] = run_events(db, run)
    assert (skipped.status, skipped.search_attempts) == (RunEventStatus.FAILED, 2)
    assert skipped.error == "the search couldn't start: Apify refused the request (400)"
    assert {e.status for e in spend(db, run)} == {SpendStatus.RELEASED}  # nothing was charged
    assert fresh(db, run).status == RunStatus.FAILED


def test_a_busy_apify_retries_the_job_later_without_using_a_search_attempt(db: Session) -> None:
    clock = Clock()
    search = StubSearch(start_error=ProviderError("Apify is busy or failing (503)", retryable=True))
    run = approved_run(db)
    worker(clock, search).run_once()
    [job] = jobs(db, COLLECT_EVENT)
    assert (job.status, job.attempts, job.last_error) == (
        JobStatus.QUEUED,
        1,
        "ProviderError: Apify is busy or failing (503)",
    )
    [waiting] = run_events(db, run)
    assert (waiting.status, waiting.search_attempts, waiting.search_queries) == (RunEventStatus.PENDING, 0, [])
    assert [e.status for e in spend(db, run)] == [SpendStatus.RELEASED]


def test_the_last_failed_attempt_closes_the_reservation_and_skips_the_event(db: Session) -> None:
    class Unreachable(StubSearch):
        def get(self, _run_id: str) -> SearchRun:
            raise ProviderError("Apify is busy or failing (503)", retryable=True)

    clock = Clock()
    run = approved_run(db)
    [job] = jobs(db, COLLECT_EVENT)
    job.max_attempts = 2
    db.commit()
    run_jobs(db, worker(clock, Unreachable()), clock)

    assert jobs(db, COLLECT_EVENT)[0].status == JobStatus.FAILED
    [entry] = spend(db, run)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, entry.estimated_usd)
    [skipped] = run_events(db, run)
    assert (skipped.status, skipped.error) == (RunEventStatus.FAILED, "the collect job failed after its last attempt")
    assert fresh(db, run).status == RunStatus.FAILED


# -------------------------------------------------------------------------------------------------- the budget
def test_the_daily_cap_makes_the_run_wait_for_the_next_day(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db)
    w = worker(clock, search, daily_cap_usd="0.10")  # a search needs $0.20
    w.run_once()
    waiting = fresh(db, run)
    assert (waiting.status, waiting.status_detail) == (RunStatus.WAITING_FOR_BUDGET, collect.WAITING)
    [pending] = run_events(db, run)
    assert (pending.status, pending.search_attempts) == (RunEventStatus.PENDING, 0)
    [job] = jobs(db, COLLECT_EVENT)
    assert job.run_after >= datetime(2026, 10, 25, tzinfo=UTC)
    assert search.started == []

    clock.now = job.run_after
    run_jobs(db, worker(clock, search), clock)  # a new day, and room under the default cap
    assert fresh(db, run).status == RunStatus.QUALIFYING


def test_an_event_the_run_budget_cannot_cover_is_skipped(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db, budget_usd="0.10")
    run_jobs(db, worker(clock, search), clock)
    [skipped] = run_events(db, run)
    assert skipped.status == RunEventStatus.FAILED
    assert skipped.error is not None
    assert skipped.error.startswith("the run's budget can't cover this search")
    assert search.started == []
    assert fresh(db, run).status == RunStatus.FAILED


# ------------------------------------------------------------------------------------------------ crash safety
def test_a_job_that_runs_again_polls_the_search_it_started(db: Session) -> None:
    clock, search = Clock(), StubSearch(polls_until_done=2)
    run = approved_run(db)
    ctx = worker(clock, search).ctx
    payload = {"run_id": str(run.id), "event_id": str(run_events(db, run)[0].event_id)}
    for _ in range(3):  # as if each attempt had died right after its step, before the job was updated
        with session_scope() as session:
            collect.collect_event(ctx, session, payload)
    assert search.started == [QUERIES]
    assert run_events(db, run)[0].status == RunEventStatus.DONE


def test_a_search_started_by_a_worker_that_died_is_counted_in_full(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db)
    [pending] = run_events(db, run)
    crashed = budget.reserve(
        db,
        provider="apify",
        estimated_usd=Decimal("0.20"),
        daily_cap_usd=Decimal(20),
        run_id=run.id,
        step_key=step_key(run.id, f"collect:{pending.event_id}"),
        now=NOW,
    )
    budget.mark_started(db, crashed, now=NOW)  # the search may have started; its id was never stored
    pending.status = RunEventStatus.SEARCHING
    pending.search_attempts = 1
    db.commit()

    run_jobs(db, worker(clock, search), clock)
    first, second = spend(db, run)
    assert (first.id, first.status, first.actual_usd) == (crashed, SpendStatus.SETTLED, Decimal("0.20"))
    assert second.status == SpendStatus.SETTLED
    assert fresh(db, run).status == RunStatus.QUALIFYING


@pytest.mark.parametrize("left_by_a_crash", [RunEventStatus.PENDING, RunEventStatus.SEARCHING])
def test_a_canceled_run_starts_no_search(db: Session, left_by_a_crash: RunEventStatus) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db)
    canceled, [stale] = fresh(db, run), run_events(db, run)
    canceled.status = RunStatus.CANCELED
    stale.status = left_by_a_crash
    db.commit()
    run_jobs(db, worker(clock, search), clock)
    assert search.started == []
    assert jobs(db, COLLECT_EVENT)[0].status == JobStatus.DONE
    assert run_events(db, run)[0].status == RunEventStatus.PENDING  # no other run waits on it


def test_a_search_already_paid_for_is_stored_even_if_the_run_was_canceled(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db)
    w = worker(clock, search)
    w.run_once()  # started
    fresh(db, run).status = RunStatus.CANCELED
    db.commit()
    run_jobs(db, w, clock)
    assert db.scalar(select(func.count()).select_from(SearchCache)) == len(QUERIES)  # in the cache for other runs
    assert [e.status for e in spend(db, run)] == [SpendStatus.SETTLED]
    assert fresh(db, run).status == RunStatus.CANCELED
    assert jobs(db, QUALIFY) == []


def test_posts_found_by_several_searches_are_stored_once(db: Session) -> None:
    clock = Clock()

    class Overlapping(StubSearch):
        def posts(self, run: SearchRun) -> list[SearchedPost]:
            [post] = super().posts(run)[:1]
            return [post, dataclasses.replace(post, query=QUERIES[1])]

    run = approved_run(db)
    run_jobs(db, worker(clock, Overlapping()), clock)
    assert db.scalar(select(func.count()).select_from(Post)) == 1
    assert db.scalars(select(EventPost.query)).all() == [QUERIES[0]]  # the first search that found it
    assert fresh(db, run).status == RunStatus.QUALIFYING


def test_a_post_with_an_implausible_id_is_left_out(db: Session) -> None:
    class OddIds(StubSearch):
        def posts(self, run: SearchRun) -> list[SearchedPost]:
            good, odd = super().posts(run)[:2]
            return [good, dataclasses.replace(odd, id="9" * 65)]

    approved_run(db)
    clock = Clock()
    run_jobs(db, worker(clock, OddIds()), clock)
    assert db.scalar(select(func.count()).select_from(Post)) == 1


def test_a_search_that_finds_nothing_is_cached_as_searched(db: Session) -> None:
    clock = Clock()
    run = approved_run(db)
    run_jobs(db, worker(clock, StubSearch(posts_per_query=0)), clock)
    assert sorted((c.query, c.post_count) for c in db.scalars(select(SearchCache))) == sorted((q, 0) for q in QUERIES)
    assert run_events(db, run)[0].status == RunEventStatus.DONE
    assert fresh(db, run).status == RunStatus.QUALIFYING  # qualify decides what an empty event means


def test_a_finished_search_whose_cost_is_unknown_is_counted_in_full(db: Session) -> None:
    class NoUsage(StubSearch):
        def get(self, run_id: str) -> SearchRun:
            return dataclasses.replace(super().get(run_id), cost_usd=None)

    clock = Clock()
    run = approved_run(db)
    run_jobs(db, worker(clock, NoUsage()), clock)
    [entry] = spend(db, run)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, entry.estimated_usd)


def test_a_search_apify_lost_track_of_is_counted_in_full_and_tried_again(db: Session) -> None:
    @dataclass
    class Forgetful(StubSearch):
        lost: bool = False

        def get(self, run_id: str) -> SearchRun:
            if not self.lost:
                self.lost = True
                raise ProviderError("Apify refused the request (404)", retryable=False)
            return super().get(run_id)

    clock, search = Clock(), Forgetful()
    run = approved_run(db)
    run_jobs(db, worker(clock, search), clock)
    assert len(search.started) == 2
    first, second = spend(db, run)
    assert (first.status, first.actual_usd) == (SpendStatus.SETTLED, first.estimated_usd)
    assert second.status == SpendStatus.SETTLED
    assert run_events(db, run)[0].status == RunEventStatus.DONE


def test_a_search_whose_posts_apify_refuses_to_hand_over_is_paid_for_and_tried_again(db: Session) -> None:
    @dataclass
    class Unreadable(StubSearch):
        refused: list[SearchRun] = field(default_factory=list)

        def posts(self, run: SearchRun) -> list[SearchedPost]:
            if not self.refused:
                self.refused.append(run)
                raise ProviderError("Apify refused the request (403)", retryable=False)
            return super().posts(run)

    clock, search = Clock(), Unreadable()
    run = approved_run(db)
    run_jobs(db, worker(clock, search), clock)
    assert len(search.started) == 2
    first, second = spend(db, run)
    assert (first.status, first.actual_usd) == (SpendStatus.SETTLED, search.refused[0].cost_usd)  # what it charged
    assert second.status == SpendStatus.SETTLED
    [done] = run_events(db, run)
    assert (done.status, done.search_attempts, done.error) == (RunEventStatus.DONE, 2, None)


def test_a_job_for_an_event_already_collected_does_nothing(db: Session) -> None:
    clock, search = Clock(), StubSearch()
    run = approved_run(db)
    w = worker(clock, search)
    run_jobs(db, w, clock)
    [done] = run_events(db, run)
    with session_scope() as session:  # e.g. a duplicate delivery of the job
        payload = {"run_id": str(run.id), "event_id": str(done.event_id)}
        assert collect.collect_event(w.ctx, session, payload) is DONE
    assert search.started == [QUERIES]


def test_the_collect_job_is_registered() -> None:
    assert JOBS[COLLECT_EVENT].on_give_up is collect.give_up
