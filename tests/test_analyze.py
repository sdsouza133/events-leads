"""The analysis steps end to end, on the fake providers: from a signed-in run to the plan the requester reviews."""

import dataclasses
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import queue
from app.config import Settings
from app.db import session_scope
from app.models import Job, JobStatus, Run, RunStatus, SpendEntry, SpendStatus
from app.pipeline import analyze
from app.pipeline.analyze import ANALYZE_SITE, FIND_EVENTS
from app.pipeline.base import JobContext
from app.pipeline.jobs import JOBS
from app.providers.fakes import FakeFetcher, FakeLlm
from app.providers.registry import build_providers
from app.providers.types import Answer, FetchedPage, ProviderError
from app.providers.webfetch import FetchError
from app.schemas import MAX_QUERIES, EventSuggestion, SiteAnalysis
from app.worker import PAUSED_RECHECK, Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class StubLlm(FakeLlm):
    """The fake model, with the costs, failures and answers a test needs."""

    def __init__(
        self,
        *,
        site_cost: str = "0",
        events_cost: str = "0",
        site_error: ProviderError | None = None,
        events_error: ProviderError | None = None,
        events: list[EventSuggestion] | None = None,
        while_analyzing: Callable[[], None] | None = None,
    ) -> None:
        self.site_cost = Decimal(site_cost)
        self.events_cost = Decimal(events_cost)
        self.site_error = site_error
        self.events_error = events_error
        self.events = events
        self.while_analyzing = while_analyzing

    def analyze_site(self, pages: list[FetchedPage]) -> Answer[SiteAnalysis]:
        if self.while_analyzing:
            self.while_analyzing()
        if self.site_error:
            raise self.site_error
        return Answer(super().analyze_site(pages).value, self.site_cost, "stub")

    def find_events(self, analysis: SiteAnalysis, *, today: date) -> Answer[list[EventSuggestion]]:
        if self.events_error:
            raise self.events_error
        found = super().find_events(analysis, today=today).value
        return Answer(found if self.events is None else self.events, self.events_cost, "stub")


def worker(*, llm: FakeLlm | None = None, fetcher: FakeFetcher | None = None, **settings: object) -> Worker:
    configured = Settings.model_validate(settings)
    providers = build_providers(configured)
    providers = dataclasses.replace(providers, llm=llm or providers.llm, fetcher=fetcher or providers.fetcher)
    return Worker(JobContext(settings=configured, providers=providers, clock=lambda: NOW), JOBS, name="worker-a")


def start(db: Session, *, max_attempts: int = 5, **run: object) -> Run:
    """A run whose requester just signed in, with its first job queued, as the sign-in page leaves it."""
    created = make_run(db, status=RunStatus.ANALYZING, **run)  # type: ignore[arg-type]
    queue.enqueue(db, ANALYZE_SITE, {"run_id": str(created.id)}, run_after=NOW, max_attempts=max_attempts)
    db.commit()
    return created


def drain(w: Worker) -> int:
    jobs = 0
    while w.run_once():
        jobs += 1
    return jobs


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found = db.get(Run, run.id)
    assert found is not None
    return found


def spend(db: Session, run: Run) -> list[SpendEntry]:
    return list(db.scalars(select(SpendEntry).where(SpendEntry.run_id == run.id).order_by(SpendEntry.id)))


def test_the_analysis_budget_must_cover_what_analyze_site_reserves() -> None:
    Settings(analysis_budget_usd=analyze.ANALYZE_SITE_ESTIMATE)
    with pytest.raises(ValidationError, match=r"greater than or equal to 0\.15"):
        Settings(analysis_budget_usd=analyze.ANALYZE_SITE_ESTIMATE - Decimal("0.01"))


def test_a_signed_in_run_becomes_a_plan_to_review(db: Session) -> None:
    run = start(db)
    assert drain(worker(llm=StubLlm(site_cost="0.06", events_cost="0.21"))) == 2  # analyze_site, then find_events

    done = fresh(db, run)
    assert (done.status, done.status_detail) == (RunStatus.AWAITING_APPROVAL, analyze.PLAN_READY)
    assert done.icp is not None
    assert SiteAnalysis.model_validate(done.icp).company_name == "Acme Analytics"
    assert done.suggested_events is not None
    assert len(done.suggested_events) == 4
    for event in done.suggested_events:
        EventSuggestion.model_validate({k: v for k, v in event.items() if k != "queries"})
        assert 1 <= len(event["queries"]) <= MAX_QUERIES
        assert event["queries"][0] == f'"{event["name"]}"'
    assert [(e.note, e.status, e.actual_usd) for e in spend(db, done)] == [
        (ANALYZE_SITE, SpendStatus.SETTLED, Decimal("0.06")),
        (FIND_EVENTS, SpendStatus.SETTLED, Decimal("0.21")),
    ]
    assert (done.spent_usd, done.reserved_usd) == (Decimal("0.27"), Decimal(0))


def test_find_events_may_use_whatever_is_left_of_the_analysis_budget(db: Session) -> None:
    run = start(db)
    drain(worker(llm=StubLlm(site_cost="0.05")))
    first, second = spend(db, fresh(db, run))
    assert first.estimated_usd == analyze.ANALYZE_SITE_ESTIMATE
    assert second.estimated_usd == Settings().analysis_budget_usd - Decimal("0.05")


def test_while_paused_the_site_is_not_read_and_nothing_is_spent(db: Session) -> None:
    run = start(db)
    fetcher = FakeFetcher()
    worker(fetcher=fetcher, pause_new_runs=True).run_once()
    assert fetcher.fetched == []
    [job] = db.scalars(select(Job).where(Job.kind == ANALYZE_SITE)).all()
    db.refresh(job)
    assert (job.status, job.attempts, job.run_after) == (JobStatus.QUEUED, 0, NOW + PAUSED_RECHECK)
    assert fresh(db, run).status == RunStatus.ANALYZING
    assert spend(db, run) == []


def test_an_unreadable_site_fails_the_run_with_a_reason_and_spends_nothing(db: Session) -> None:
    run = start(db, website_url="https://acme.invalid/")
    drain(worker())
    failed = fresh(db, run)
    assert failed.status == RunStatus.FAILED
    assert failed.status_detail == "We couldn't read acme.example: example.invalid could not be found."
    assert spend(db, failed) == []


def test_a_site_with_almost_no_text_is_not_guessed_at(db: Session) -> None:
    run = start(db)
    empty = FakeFetcher(pages={"https://acme.example/": "<html><head><title>Acme</title></head><body>Hi</body></html>"})
    drain(worker(fetcher=empty))
    failed = fresh(db, run)
    assert failed.status == RunStatus.FAILED
    assert failed.status_detail is not None
    assert failed.status_detail.startswith("We couldn't find enough text on acme.example")
    assert spend(db, failed) == []


def test_follow_up_pages_that_fail_are_skipped() -> None:
    class Flaky(FakeFetcher):
        def fetch(self, url: str) -> FetchedPage:
            if url.endswith("/pricing"):
                raise FetchError("the site answered with an error (500)")
            return super().fetch(url)

    pages = analyze.read_site(Flaky(), "https://acme.example/")
    assert [p.url for p in pages] == [
        "https://acme.example/",
        "https://acme.example/customers",
        "https://acme.example/about",
    ]


def test_a_refusal_fails_the_run_and_counts_what_it_billed(db: Session) -> None:
    run = start(db)
    refusal = ProviderError("Claude declined this request", retryable=False, cost_usd=Decimal("0.02"))
    drain(worker(llm=StubLlm(site_error=refusal)))
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail, failed.error) == (
        RunStatus.FAILED,
        analyze.NOT_ANALYZED,
        "Claude declined this request",
    )
    [entry] = spend(db, failed)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, Decimal("0.02"))


def test_an_outage_is_retried_and_the_run_fails_once_attempts_run_out(db: Session) -> None:
    outage = ProviderError("the Claude API answered 529", retryable=True)
    run = start(db, max_attempts=2)
    w = worker(llm=StubLlm(site_error=outage))
    assert w.run_once()
    assert fresh(db, run).status == RunStatus.ANALYZING  # waits for a retry
    [first] = db.scalars(select(Job)).all()
    assert (first.status, first.attempts) == (JobStatus.QUEUED, 1)

    later = worker(llm=StubLlm(site_error=outage))
    later.ctx = dataclasses.replace(later.ctx, clock=lambda: NOW + timedelta(hours=1))
    assert later.run_once()
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, analyze.SOMETHING_WRONG)
    assert [e.status for e in spend(db, failed)] == [SpendStatus.RELEASED, SpendStatus.RELEASED]  # nothing billed


def test_the_daily_cap_makes_the_run_wait_until_tomorrow(db: Session) -> None:
    run = start(db)
    drain(worker(daily_cap_usd="0.10"))
    waiting = fresh(db, run)
    assert (waiting.status, waiting.status_detail) == (RunStatus.ANALYZING, analyze.WAITING)
    [job] = db.scalars(select(Job)).all()
    tomorrow = datetime(2026, 10, 25, tzinfo=UTC)
    assert job.status == JobStatus.QUEUED
    assert tomorrow <= job.run_after <= tomorrow + timedelta(minutes=15)
    assert job.attempts == 0
    assert spend(db, waiting) == []


def test_a_run_whose_analysis_budget_is_used_up_stops_before_finding_events(db: Session) -> None:
    run = start(db)
    drain(worker(llm=StubLlm(site_cost="0.18"), analysis_budget_usd="0.20"))
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, analyze.OVER_BUDGET)
    assert len(spend(db, failed)) == 1  # find_events never reserved anything


def test_no_events_found_still_gives_a_plan_to_fill_in(db: Session) -> None:
    run = start(db)
    drain(worker(llm=StubLlm(events=[])))
    plan = fresh(db, run)
    assert (plan.status, plan.status_detail, plan.suggested_events) == (
        RunStatus.AWAITING_APPROVAL,
        analyze.NO_EVENTS,
        [],
    )


def test_running_the_steps_again_changes_nothing(db: Session) -> None:
    run = start(db)
    drain(worker())
    before = fresh(db, run)
    icp, events = before.icp, before.suggested_events
    for kind in (ANALYZE_SITE, FIND_EVENTS):  # e.g. a job retried after its first attempt had finished
        queue.enqueue(db, kind, {"run_id": str(run.id)}, run_after=NOW)
    db.commit()
    assert drain(worker()) == 2
    after = fresh(db, run)
    assert (after.icp, after.suggested_events, after.status) == (icp, events, RunStatus.AWAITING_APPROVAL)
    assert len(spend(db, after)) == 2


def test_a_run_canceled_during_the_analysis_is_left_canceled_but_the_cost_is_counted(db: Session) -> None:
    run = start(db)

    def cancel() -> None:
        with session_scope() as session:
            canceled = session.get(Run, run.id)
            assert canceled is not None
            canceled.status = RunStatus.CANCELED

    drain(worker(llm=StubLlm(site_cost="0.04", while_analyzing=cancel)))
    after = fresh(db, run)
    assert (after.status, after.icp) == (RunStatus.CANCELED, None)
    [entry] = spend(db, after)
    assert (entry.status, entry.actual_usd) == (SpendStatus.SETTLED, Decimal("0.04"))


def test_give_up_only_fails_a_run_that_is_still_analyzing(db: Session) -> None:
    ready = make_run(db, status=RunStatus.AWAITING_APPROVAL)
    db.commit()
    w = worker()
    analyze.give_up(w.ctx, db, {"run_id": str(ready.id)})
    assert ready.status == RunStatus.AWAITING_APPROVAL


def test_a_refusal_while_finding_events_fails_the_run_after_the_site_was_analyzed(db: Session) -> None:
    run = start(db)
    refusal = ProviderError("Claude's answer was cut off before it finished", retryable=False, cost_usd=Decimal("0.3"))
    drain(worker(llm=StubLlm(site_cost="0.05", events_error=refusal)))
    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, analyze.NOT_ANALYZED)
    assert failed.icp is not None  # the first step's answer is kept
    assert [e.actual_usd for e in spend(db, failed)] == [Decimal("0.05"), Decimal("0.3")]


def test_an_outage_while_finding_events_is_retried(db: Session) -> None:
    run = start(db)
    outage = ProviderError("couldn't reach the Claude API", retryable=True, cost_usd=None)
    drain(worker(llm=StubLlm(events_error=outage)))
    waiting = fresh(db, run)
    assert waiting.status == RunStatus.ANALYZING
    jobs = {job.kind: job for job in db.scalars(select(Job))}
    assert (jobs[FIND_EVENTS].status, jobs[FIND_EVENTS].attempts) == (JobStatus.QUEUED, 1)
    # the dropped call may have been billed: its reservation is counted in full
    assert spend(db, waiting)[-1].actual_usd == spend(db, waiting)[-1].estimated_usd


def test_the_daily_cap_can_also_stop_the_second_step(db: Session) -> None:
    run = start(db)
    first = worker(llm=StubLlm(site_cost="0.10"))
    assert first.run_once()  # analyze_site
    assert worker(daily_cap_usd="0.30").run_once()  # 0.20 left today, find_events wants up to 0.40
    waiting = fresh(db, run)
    assert (waiting.status, waiting.status_detail) == (RunStatus.ANALYZING, analyze.WAITING)
