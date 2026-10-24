"""Qualifying a run's posts (issue #6): free rules first, Claude for the rest, labels shared, candidates screened."""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import queue
from app.config import Settings
from app.models import (
    Event,
    EventPost,
    Job,
    JobStatus,
    Lead,
    Person,
    Post,
    PostLabel,
    Run,
    RunEvent,
    RunEventStatus,
    RunStatus,
    SpendEntry,
    SpendStatus,
)
from app.pipeline import qualify
from app.pipeline.base import JobContext
from app.pipeline.jobs import JOBS
from app.pipeline.qualify import ENRICH, QUALIFY
from app.providers.fakes import FakeLlm
from app.providers.registry import build_providers
from app.providers.types import Answer, FetchedPage, ProviderError
from app.schemas import LabeledPost, PostToLabel
from app.worker import Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ANALYSIS = FakeLlm().analyze_site([FetchedPage("https://acme.example/", 200, "Acme Analytics | Home", None, "")]).value
_ids = itertools.count(7_000_000_000_000_000_000)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class CountingLlm(FakeLlm):
    """The fake model, recording each batch it was asked to label, at a fixed cost per call."""

    def __init__(self, *, cost: str = "0.05", error: ProviderError | None = None) -> None:
        self.batches: list[list[str]] = []
        self.cost = Decimal(cost)
        self.error = error

    def label_posts(self, event_name: str, posts: list[PostToLabel]) -> Answer[list[LabeledPost]]:
        if self.error is not None:
            raise self.error
        self.batches.append([post.post_id for post in posts])
        return Answer(super().label_posts(event_name, posts).value, self.cost, "stub")


def worker(clock: Clock, llm: FakeLlm, **settings: object) -> Worker:
    configured = Settings.model_validate(settings)
    providers = dataclasses.replace(build_providers(configured), llm=llm)
    only_this_step = {QUALIFY: JOBS[QUALIFY]}  # the next step's job stays queued for its own tests
    return Worker(JobContext(settings=configured, providers=providers, clock=clock), only_this_step, name="worker-a")


def event(db: Session, name: str = "Pipeline Summit") -> Event:
    found = Event(key=name.lower().replace(" ", "-"), name=name)
    db.add(found)
    db.flush()
    return found


def post(
    db: Session,
    on: Event,
    text: str,
    *,
    author: str = "jane-doe",
    headline: str | None = "VP Marketing at Northwind Labs",
    company_page: bool = False,
    posted_at: datetime = NOW,
) -> str:
    post_id = str(next(_ids))
    url = f"https://www.linkedin.com/company/{author}" if company_page else f"https://www.linkedin.com/in/{author}"
    db.add(
        Post(
            id=post_id,
            url=f"https://www.linkedin.com/posts/{author}-activity-{post_id}",
            author_name=author.replace("-", " ").title(),
            author_url=url,
            author_headline=None if company_page else headline,
            author_kind="company" if company_page else "person",
            text=text,
            posted_at=posted_at,
        )
    )
    db.flush()
    db.add(EventPost(event_id=on.id, post_id=post_id, query=f'"{on.name}"'))
    db.flush()
    return post_id


def qualifying_run(db: Session, *events: Event, budget_usd: str = "3.00", max_attempts: int = 5) -> Run:
    """A run whose posts were just collected, as collect leaves it."""
    run = make_run(db, status=RunStatus.QUALIFYING, budget_usd=Decimal(budget_usd))
    run.icp = ANALYSIS.model_dump(mode="json")
    for on in events:
        db.add(RunEvent(run_id=run.id, event_id=on.id, source="suggested", queries=[], status=RunEventStatus.DONE))
    queue.enqueue(db, QUALIFY, {"run_id": str(run.id)}, run_after=NOW, max_attempts=max_attempts)
    db.commit()
    return run


def run_jobs(db: Session, w: Worker, clock: Clock, *, max_steps: int = 40) -> None:
    """Run the worker until no qualify job is waiting, moving the clock to each deferred job's time."""
    for _ in range(max_steps):
        if w.run_once():
            continue
        db.expire_all()
        due = db.scalar(select(func.min(Job.run_after)).where(Job.kind == QUALIFY, Job.status == JobStatus.QUEUED))
        if due is None:
            return
        clock.now = max(clock.now, due)
    raise AssertionError("qualify jobs are still waiting")


def labels(db: Session) -> dict[str, tuple[str, str, str | None]]:
    db.expire_all()
    return {row.post_id: (row.label, row.method, row.model) for row in db.scalars(select(PostLabel))}


def leads(db: Session, run: Run) -> list[Lead]:
    db.expire_all()
    return list(db.scalars(select(Lead).where(Lead.run_id == run.id)))


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found = db.get(Run, run.id)
    assert found is not None
    return found


def spend(db: Session, run: Run) -> list[SpendEntry]:
    db.expire_all()
    return list(db.scalars(select(SpendEntry).where(SpendEntry.run_id == run.id).order_by(SpendEntry.id)))


def unclear_posts(db: Session, on: Event, count: int) -> list[str]:
    return [
        post(db, on, f"Great lineup at Pipeline Summit this year, part {i}.", author=f"p-{i}") for i in range(count)
    ]


# -------------------------------------------------------------------------------------------------------- labels
def test_obvious_posts_are_labeled_by_rules_and_the_rest_by_claude(db: Session) -> None:
    summit = event(db)
    going = post(db, summit, "Heading to Pipeline Summit next week!", author="ana")
    booth = post(db, summit, "Our booth crew is ready for Pipeline Summit.", author="ben")  # the rules aren't sure
    run = qualifying_run(db, summit)
    llm, clock = CountingLlm(), Clock()
    run_jobs(db, worker(clock, llm), clock)

    assert labels(db) == {going: ("attending", "rule", None), booth: ("exhibiting", "model", "stub")}
    assert llm.batches == [[booth]]
    [entry] = spend(db, run)
    assert (entry.provider, entry.status, entry.actual_usd, entry.units) == (
        "claude",
        SpendStatus.SETTLED,
        Decimal("0.05"),
        Decimal(1),
    )


def test_labels_are_reused_by_the_next_run_for_the_same_event(db: Session) -> None:
    summit = event(db)
    unclear_posts(db, summit, 3)
    llm, clock = CountingLlm(), Clock()
    qualifying_run(db, summit)
    run_jobs(db, worker(clock, llm), clock)
    second = qualifying_run(db, summit)
    run_jobs(db, worker(clock, llm), clock)
    assert len(llm.batches) == 1  # the second run paid for nothing
    assert spend(db, second) == []


def test_claude_labels_at_most_25_posts_per_call(db: Session) -> None:
    summit = event(db)
    unclear_posts(db, summit, 30)
    qualifying_run(db, summit)
    llm, clock = CountingLlm(), Clock()
    run_jobs(db, worker(clock, llm), clock)
    assert [len(batch) for batch in llm.batches] == [25, 5]


def test_the_job_takes_turns_between_calls(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualify, "CALLS_PER_TURN", 1)
    summit = event(db)
    unclear_posts(db, summit, 60)
    run = qualifying_run(db, summit)
    llm, clock = CountingLlm(), Clock()
    w = worker(clock, llm)
    w.run_once()
    assert len(llm.batches) == 1
    assert fresh(db, run).status == RunStatus.QUALIFYING  # deferred to itself, not done
    run_jobs(db, w, clock)
    assert [len(batch) for batch in llm.batches] == [25, 25, 10]
    assert fresh(db, run).status == RunStatus.ENRICHING


def test_company_pages_are_never_labeled(db: Session) -> None:
    summit = event(db)
    page = post(db, summit, "Visit us at booth 4 at Pipeline Summit!", author="northwind", company_page=True)
    run = qualifying_run(db, summit)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm()), clock)
    assert page not in labels(db)
    assert leads(db, run) == []


# ---------------------------------------------------------------------------------------------------- candidates
def test_only_people_who_were_there_become_candidates(db: Session) -> None:
    summit = event(db)
    post(db, summit, "We're hosting Pipeline Summit and the agenda is live!", author="host")
    post(db, summit, "Use my code PS20 for 20% off Pipeline Summit tickets.", author="seller")
    post(db, summit, "Can't make it to Pipeline Summit this year.", author="away")
    post(db, summit, "Great lineup at Pipeline Summit this year.", author="unsure")
    going = post(db, summit, "See you at Pipeline Summit!", author="ana")
    run = qualifying_run(db, summit)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm()), clock)

    [lead] = leads(db, run)
    assert (lead.post_id, lead.attendance, lead.event_id, lead.included, lead.exclude_reason) == (
        going,
        "attending",
        summit.id,
        False,  # enrich decides who is on the list
        None,
    )
    person = db.get(Person, lead.person_id)
    assert person is not None
    assert (person.linkedin_url, person.name) == ("https://www.linkedin.com/in/ana", "Ana")


def test_each_person_is_one_candidate_with_their_most_telling_post(db: Session) -> None:
    summit, forum = event(db), event(db, "RevOps Forum")
    post(db, summit, "Back from Pipeline Summit, what a week.", author="ana", posted_at=NOW)
    speaking = post(
        db, summit, "I'm speaking at Pipeline Summit on Tuesday.", author="ana", posted_at=NOW - timedelta(days=9)
    )
    post(db, forum, "Heading to RevOps Forum!", author="ana")
    run = qualifying_run(db, summit, forum)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm()), clock)

    [lead] = leads(db, run)
    assert (lead.post_id, lead.attendance) == (speaking, "speaking")


def test_headlines_that_cannot_match_are_kept_out_with_the_reason(db: Session) -> None:
    summit = event(db)
    post(db, summit, "Heading to Pipeline Summit!", author="sam", headline="MBA Student at Wharton")
    post(db, summit, "Heading to Pipeline Summit!", author="col", headline="VP Marketing at Acme Analytics")
    post(db, summit, "Heading to Pipeline Summit!", author="ana", headline="Head of Growth at Bluefin")
    run = qualifying_run(db, summit)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm()), clock)

    reasons = {db.get(Person, lead.person_id).name: lead.exclude_reason for lead in leads(db, run)}  # type: ignore[union-attr]
    assert reasons == {
        "Sam": "headline says 'student'",
        "Col": "works at the requester's company",
        "Ana": None,
    }


def test_the_run_moves_on_to_enrich(db: Session) -> None:
    summit = event(db)
    post(db, summit, "See you at Pipeline Summit!", author="ana")
    run = qualifying_run(db, summit)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm()), clock)
    moved = fresh(db, run)
    assert (moved.status, moved.status_detail, moved.error) == (RunStatus.ENRICHING, qualify.ENRICHING, None)
    db.expire_all()
    [enrich] = db.scalars(select(Job).where(Job.kind == ENRICH)).all()
    assert enrich.payload == {"run_id": str(run.id)}


# ------------------------------------------------------------------------------------------- budget and failures
def test_the_daily_cap_pauses_labeling_until_the_next_day(db: Session) -> None:
    summit = event(db)
    going = post(db, summit, "See you at Pipeline Summit!", author="ana")
    unclear_posts(db, summit, 2)
    run = qualifying_run(db, summit)
    llm, clock = CountingLlm(), Clock()
    worker(clock, llm, daily_cap_usd="0.01").run_once()

    waiting = fresh(db, run)
    assert (waiting.status, waiting.status_detail) == (RunStatus.QUALIFYING, qualify.WAITING)
    assert llm.batches == []
    assert going in labels(db)  # the free rules ran anyway
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == QUALIFY)).all()
    assert job.run_after >= datetime(2026, 10, 25, tzinfo=UTC)


def test_when_the_run_budget_runs_out_it_goes_on_with_what_is_labeled(db: Session) -> None:
    summit = event(db)
    post(db, summit, "See you at Pipeline Summit!", author="ana")
    unclear_posts(db, summit, 2)
    run = qualifying_run(db, summit, budget_usd="0.01")
    llm, clock = CountingLlm(), Clock()
    run_jobs(db, worker(clock, llm), clock)

    assert llm.batches == []
    assert len(leads(db, run)) == 1
    moved = fresh(db, run)
    assert moved.status == RunStatus.ENRICHING
    assert moved.error is not None
    assert "budget" in moved.error


def test_posts_claude_declines_to_label_are_kept_as_unclear(db: Session) -> None:
    summit = event(db)
    [refused] = unclear_posts(db, summit, 1)
    run = qualifying_run(db, summit)
    declined = ProviderError("Claude declined this request", retryable=False, cost_usd=Decimal("0.01"), declined=True)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm(error=declined)), clock)

    assert labels(db) == {refused: ("unclear", "skipped", None)}
    assert [(e.status, e.actual_usd) for e in spend(db, run)] == [(SpendStatus.SETTLED, Decimal("0.01"))]
    assert fresh(db, run).status == RunStatus.ENRICHING


def test_a_request_claude_rejects_fails_the_run_and_labels_nothing(db: Session) -> None:
    summit = event(db)
    unclear_posts(db, summit, 1)
    run = qualifying_run(db, summit)
    rejected = ProviderError("the Claude API answered 401", retryable=False)  # a revoked key, a retired model
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm(error=rejected)), clock)

    assert labels(db) == {}  # nothing cached: once the key is fixed, the next run labels these posts for real
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, qualify.SOMETHING_WRONG)
    assert all(entry.status != SpendStatus.RESERVED for entry in spend(db, run))


def test_the_last_failed_attempt_fails_the_run(db: Session) -> None:
    summit = event(db)
    unclear_posts(db, summit, 1)
    run = qualifying_run(db, summit, max_attempts=1)
    busy = ProviderError("Claude is busy or failing (529)", retryable=True)
    clock = Clock()
    run_jobs(db, worker(clock, CountingLlm(error=busy)), clock)

    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, qualify.SOMETHING_WRONG)
    assert all(entry.status != SpendStatus.RESERVED for entry in spend(db, run))


def test_a_run_no_longer_qualifying_is_left_alone(db: Session) -> None:
    summit = event(db)
    unclear_posts(db, summit, 1)
    run = qualifying_run(db, summit)
    fresh(db, run).status = RunStatus.CANCELED
    db.commit()
    llm, clock = CountingLlm(), Clock()
    run_jobs(db, worker(clock, llm), clock)
    assert llm.batches == []
    assert labels(db) == {}


def test_the_qualify_job_is_registered() -> None:
    assert JOBS[QUALIFY].on_give_up is qualify.give_up
