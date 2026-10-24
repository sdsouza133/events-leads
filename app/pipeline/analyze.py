"""A run's first two jobs (issue #4), between sign-in and the plan the requester approves.

1. `analyze_site`: read the home page and up to four pages that say who the customers are, then ask Claude for the
   company's ICP and the themes of the events its buyers attend.
2. `find_events`: ask Claude, searching the web live, for those events, and build the LinkedIn searches for each.

They are the only paid work before approval, so together they stay within the analysis budget: `analyze_site`
reserves a fixed estimate, and `find_events` reserves whatever is left of the budget after it.
"""

import logging
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app import budget, queue
from app.budget import BudgetExceededError
from app.models import Run, RunStatus
from app.pipeline import runs
from app.pipeline.base import DONE, Defer, JobContext, Outcome, Payload
from app.pipeline.keywords import build_queries
from app.pipeline.paid import paid_call, recover_step
from app.providers.protocols import WebFetcher
from app.providers.types import FetchedPage, ProviderError
from app.providers.webfetch import FetchError, pick_followup_links
from app.schemas import SiteAnalysis

log = logging.getLogger(__name__)

ANALYZE_SITE = "analyze_site"
FIND_EVENTS = "find_events"
MAX_PAGES = 5
MIN_TEXT_CHARS = 300
ANALYZE_SITE_ESTIMATE = Decimal("0.15")
FIND_EVENTS_MIN_ESTIMATE = Decimal("0.10")

UNREADABLE = "We couldn't read {domain}: {reason}."
TOO_LITTLE_TEXT = (
    "We couldn't find enough text on {domain} to tell what you sell. If the site is built entirely with JavaScript, "
    "try the address of a page with written content, such as your About page."
)
NOT_ANALYZED = "We couldn't analyze this website automatically."
OVER_BUDGET = "This request reached its spending limit before the plan was ready."
WAITING = "We've reached today's capacity. Your plan will be prepared automatically after midnight UTC."
FINDING_EVENTS = "Finding the events your buyers attend"
PLAN_READY = "Your plan is ready to review."
NO_EVENTS = "We didn't find events for your buyers automatically. Add the events you know, and we'll search those."
SOMETHING_WRONG = "Something went wrong while preparing your plan. We've been notified."


def analyze_site(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    run_id = uuid.UUID(payload["run_id"])
    run = session.get(Run, run_id)
    if run is None or run.status != RunStatus.ANALYZING or run.icp is not None:
        return DONE  # gone, canceled, or finished by an earlier attempt
    website, domain = run.website_url, run.domain
    session.commit()  # no transaction stays open while the site and Claude answer

    pages, problem = _read_or_explain(ctx.providers.fetcher, website, domain)
    if problem is not None:
        return _fail(ctx, session, run_id, problem)

    try:
        paid = paid_call(
            ctx,
            run_id=run_id,
            step=ANALYZE_SITE,
            provider="claude",
            estimate=ANALYZE_SITE_ESTIMATE,
            run_cap_usd=ctx.settings.analysis_budget_usd,
            call=lambda: ctx.providers.llm.analyze_site(pages),
        )
    except BudgetExceededError as exc:
        return _wait_or_fail(ctx, session, run_id, exc)
    except ProviderError as exc:
        if exc.retryable:
            raise  # the worker retries with backoff, then gives up (see give_up)
        return _fail(ctx, session, run_id, NOT_ANALYZED, error=str(exc))

    # Settle first: it locks the budget day and then the run, the order every transaction must take them in.
    budget.settle(session, paid.spend_id, paid.answer.cost_usd, now=ctx.now())  # paid for, whatever the run did since
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.ANALYZING:
        return DONE
    run.icp = paid.answer.value.model_dump(mode="json")
    run.status_detail = FINDING_EVENTS
    queue.enqueue(
        session, FIND_EVENTS, {"run_id": str(run_id)}, run_after=ctx.now(), dedupe_key=f"{FIND_EVENTS}:{run_id}"
    )
    return DONE


def find_events(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    run_id = uuid.UUID(payload["run_id"])
    recover_step(ctx, run_id, FIND_EVENTS)  # before reading what's left of the budget
    run = session.get(Run, run_id)
    if run is None or run.status != RunStatus.ANALYZING or run.icp is None or run.suggested_events is not None:
        return DONE
    analysis = SiteAnalysis.model_validate(run.icp)
    left = ctx.settings.analysis_budget_usd - run.spent_usd - run.reserved_usd
    session.commit()

    if left < FIND_EVENTS_MIN_ESTIMATE:
        return _fail(ctx, session, run_id, OVER_BUDGET, error=f"${left} of the analysis budget left for find_events")
    today = ctx.today()
    try:
        paid = paid_call(
            ctx,
            run_id=run_id,
            step=FIND_EVENTS,
            provider="claude",
            estimate=left,
            run_cap_usd=ctx.settings.analysis_budget_usd,
            call=lambda: ctx.providers.llm.find_events(analysis, today=today),
        )
    except BudgetExceededError as exc:
        return _wait_or_fail(ctx, session, run_id, exc)
    except ProviderError as exc:
        if exc.retryable:
            raise
        return _fail(ctx, session, run_id, NOT_ANALYZED, error=str(exc))

    suggestions = [
        {**event.model_dump(mode="json"), "queries": build_queries(event, today=today)} for event in paid.answer.value
    ]
    budget.settle(session, paid.spend_id, paid.answer.cost_usd, now=ctx.now())  # budget day, then run (as above)
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.ANALYZING:
        return DONE
    run.suggested_events = suggestions
    run.status = RunStatus.AWAITING_APPROVAL
    run.status_detail = PLAN_READY if suggestions else NO_EVENTS
    return DONE


def give_up(ctx: JobContext, session: Session, payload: Payload) -> None:
    """Both jobs failed for good (retries used up): the run can't go on."""
    run = runs.locked(session, uuid.UUID(payload["run_id"]))
    if run is not None and run.status == RunStatus.ANALYZING:
        runs.fail(run, SOMETHING_WRONG, now=ctx.now(), error="analysis job failed after its last attempt")


def read_site(fetcher: WebFetcher, url: str) -> list[FetchedPage]:
    """The home page and up to four pages it links to that describe the customers. Without the home page there is
    nothing to go on, so its FetchError propagates; a follow-up page that fails is skipped."""
    home = fetcher.fetch(url)
    pages = [home]
    for link in pick_followup_links(home, limit=MAX_PAGES - 1):
        try:
            pages.append(fetcher.fetch(link))
        except FetchError as exc:
            log.info("skipped a page that couldn't be read", extra={"reason": str(exc)})
    return pages


def _read_or_explain(fetcher: WebFetcher, website: str, domain: str) -> tuple[list[FetchedPage], str | None]:
    """The pages, or why there is nothing to analyze, in words for the requester."""
    try:
        pages = read_site(fetcher, website)
    except FetchError as exc:
        return [], UNREADABLE.format(domain=domain, reason=str(exc).rstrip("."))
    if sum(len(page.text) for page in pages) < MIN_TEXT_CHARS:
        return pages, TOO_LITTLE_TEXT.format(domain=domain)
    return pages, None


def _fail(ctx: JobContext, session: Session, run_id: uuid.UUID, detail: str, *, error: str | None = None) -> Outcome:
    run = runs.locked(session, run_id)
    if run is not None and run.status == RunStatus.ANALYZING:
        runs.fail(run, detail, now=ctx.now(), error=error)
    return DONE


def _wait_or_fail(ctx: JobContext, session: Session, run_id: uuid.UUID, exc: BudgetExceededError) -> Outcome:
    if exc.scope == "run":
        return _fail(ctx, session, run_id, OVER_BUDGET, error=str(exc))
    run = runs.locked(session, run_id)
    if run is not None and run.status == RunStatus.ANALYZING:
        run.status_detail = WAITING
    return Defer(runs.until_tomorrow(ctx.now()))
