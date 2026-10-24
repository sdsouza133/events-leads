"""A run's page: where it stands, for its requester (or an operator) only (issue #8).

While the run awaits approval, the page is its plan (`app/web/plan.py`); while it works, it shows its progress as
a few steps, and once the search is under way, what it has found so far; once it has finished, it is the list
(`app/web/results.py`).
"""

import uuid
from dataclasses import asdict, dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import Select, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.models import EventPost, Lead, Person, Post, PostLabel, Run, RunEvent, RunEventStatus, RunStatus
from app.pipeline import analyze, collect, qualify
from app.schemas import QUALIFYING_LABELS
from app.web import plan, results, start
from app.web.common import AppSettings, DbSession, SignedIn, page, visible_run

router = APIRouter()

LABELS = {
    RunStatus.AWAITING_SIGN_IN: "Waiting for you to sign in",
    RunStatus.ANALYZING: "Getting your plan ready",
    RunStatus.AWAITING_APPROVAL: "Your plan is ready",
    RunStatus.COLLECTING: "Finding your people",
    RunStatus.WAITING_FOR_BUDGET: "Finding your people",
    RunStatus.QUALIFYING: "Finding your people",
    RunStatus.ENRICHING: "Finding your people",
    RunStatus.READY: "Your list is ready",
    RunStatus.FAILED: "This run stopped",
    RunStatus.CANCELED: "This run stopped",
    RunStatus.EXPIRED: "These results have expired",
}
WORKING = frozenset(
    {
        RunStatus.ANALYZING,
        RunStatus.COLLECTING,
        RunStatus.WAITING_FOR_BUDGET,
        RunStatus.QUALIFYING,
        RunStatus.ENRICHING,
    }
)
"""While a run is in one of these, its page checks back every few seconds."""

SEARCH_STEPS = ["Searching LinkedIn posts about your events", "Checking who's really going", "Finding verified emails"]
SEARCH_STEP = {
    RunStatus.COLLECTING: 0,
    RunStatus.WAITING_FOR_BUDGET: 0,
    RunStatus.QUALIFYING: 1,
    RunStatus.ENRICHING: 2,
}
USUAL_DETAILS = frozenset(
    {start.READING_SITE, analyze.FINDING_EVENTS, collect.SEARCHING, collect.READING_POSTS, qualify.ENRICHING}
)
"""What a working run says while all is well: the steps already show it. Anything else (waiting for capacity, say)
is worth a line of its own."""


@dataclass(frozen=True)
class Progress:
    phase: str  # "plan" until the plan is ready, then "search": the page reloads when it changes
    steps: list[str]
    step: int  # the one under way; those before it are done
    note: str


@dataclass(frozen=True)
class Found:
    """What the search has found so far: the page's three counters, and how far along it is."""

    posts: int  # found for the run's events
    people: int  # whose posts show them at one of the events
    emails: int  # verified, of the people still in the running
    percent: int


def progress(run: Run) -> Progress | None:
    """How far a working run has got, as steps; None for one that isn't working."""
    note = "" if run.status_detail in USUAL_DETAILS else (run.status_detail or "")
    if run.status == RunStatus.ANALYZING:
        steps = [f"Reading {run.domain}", "Finding events your buyers go to"]
        return Progress("plan", steps, 0 if run.icp is None else 1, note)
    if run.status in SEARCH_STEP:
        return Progress("search", SEARCH_STEPS, SEARCH_STEP[run.status], note)
    return None


def found_so_far(session: Session, run: Run, accepted: tuple[str, ...]) -> Found:
    """The counters for a run in its search, and how far along it is: each of its three steps is a third of the way,
    and within a step, the share of events searched, of posts read, or of people looked up."""
    events = select(RunEvent.event_id).where(RunEvent.run_id == run.id)
    posts = _count(select(func.count(distinct(EventPost.post_id))).where(EventPost.event_id.in_(events)), session)
    people = _count(
        select(func.count(distinct(Post.author_url)))
        .join(PostLabel, PostLabel.post_id == Post.id)
        .where(
            PostLabel.event_id.in_(events),
            PostLabel.label.in_([str(label) for label in QUALIFYING_LABELS]),
            Post.author_kind == "person",
        ),
        session,
    )
    leads = select(func.count()).select_from(Lead).join(Person, Person.id == Lead.person_id)
    leads = leads.where(Lead.run_id == run.id)
    emails = _count(leads.where(Lead.exclude_reason.is_(None), Person.email_status.in_(accepted)), session)

    step = SEARCH_STEP.get(run.status, 0)
    if step == 0:  # searching: the events done (or given up on)
        total = select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run.id)
        done = total.where(RunEvent.status.in_([RunEventStatus.DONE, RunEventStatus.FAILED]))
        share = _share(_count(done, session), _count(total, session))
    elif step == 1:  # reading: the posts by people labeled
        posts_by_people = (
            select(func.count())
            .select_from(EventPost)
            .join(Post, Post.id == EventPost.post_id)
            .where(EventPost.event_id.in_(events), Post.author_kind == "person")
        )
        labeled = select(func.count()).select_from(PostLabel).where(PostLabel.event_id.in_(events))
        share = _share(_count(labeled, session), _count(posts_by_people, session))
    else:  # finding emails: the people looked up, of those not ruled out before
        to_look_up = leads.where(or_(Lead.exclude_reason.is_(None), Person.enriched_at.is_not(None)))
        share = _share(_count(leads.where(Person.enriched_at.is_not(None)), session), _count(to_look_up, session))
    return Found(posts, people, emails, percent=max(2, min(99, round((step + share) * 100 / len(SEARCH_STEPS)))))


def _count(query: Select[int], session: Session) -> int:
    return session.scalar(query) or 0


def _share(part: int, whole: int) -> float:
    return min(1.0, part / whole) if whole else 0.0


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(
    request: Request,
    run_id: uuid.UUID,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    notice: str = "",
    only: str = "",
) -> HTMLResponse:
    if account is None:
        return page(request, "sign_in_needed.html", {"account": None}, settings, status_code=401)
    run = visible_run(session, run_id, account, settings)
    if run is None:
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    if run.status == RunStatus.AWAITING_APPROVAL:
        return plan.show(request, run, account, settings, notice=notice)
    if run.status in (RunStatus.READY, RunStatus.EXPIRED):
        return results.show(request, session, run, account, settings, only=only)
    found = progress(run)
    context = {
        "account": account,
        "run": run,
        "label": LABELS[run.status],
        "progress": found,
        "found": _found(session, run, settings.accepted_email_statuses, found),
        "events": session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run.id)) or 0,
    }
    return page(request, "run.html", context, settings)


@router.get("/runs/{run_id}/status", response_model=None)
def run_status(run_id: uuid.UUID, *, session: DbSession, settings: AppSettings, account: SignedIn) -> Response:
    """What the run page polls while the run works."""
    run = visible_run(session, run_id, account, settings)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404, headers={"Cache-Control": "no-store"})
    steps = progress(run)
    found = _found(session, run, settings.accepted_email_statuses, steps)
    body = {
        "status": str(run.status),
        "label": LABELS[run.status],
        "detail": run.status_detail or "",
        "progress": asdict(steps) if steps else None,
        "found": asdict(found) if found else None,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _found(session: Session, run: Run, accepted: tuple[str, ...], steps: Progress | None) -> Found | None:
    """The counters, while the run searches; None before (making its plan) and after."""
    return found_so_far(session, run, accepted) if steps is not None and steps.phase == "search" else None
