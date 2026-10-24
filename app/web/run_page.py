"""A run's page: where it stands, for its requester (or an operator) only (issue #8).

While the run awaits approval, the page is its plan (`app/web/plan.py`); while it works, it shows its progress as
a few steps; once it has finished, it is the list (`app/web/results.py`).
"""

import uuid
from dataclasses import asdict, dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.models import Run, RunStatus
from app.pipeline import analyze, collect, qualify
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


def progress(run: Run) -> Progress | None:
    """How far a working run has got, as steps; None for one that isn't working."""
    note = "" if run.status_detail in USUAL_DETAILS else (run.status_detail or "")
    if run.status == RunStatus.ANALYZING:
        steps = [f"Reading {run.domain}", "Finding events your buyers go to"]
        return Progress("plan", steps, 0 if run.icp is None else 1, note)
    if run.status in SEARCH_STEP:
        return Progress("search", SEARCH_STEPS, SEARCH_STEP[run.status], note)
    return None


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(
    request: Request,
    run_id: uuid.UUID,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    notice: str = "",
) -> HTMLResponse:
    if account is None:
        return page(request, "sign_in_needed.html", {"account": None}, settings, status_code=401)
    run = visible_run(session, run_id, account, settings)
    if run is None:
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    if run.status == RunStatus.AWAITING_APPROVAL:
        return plan.show(request, run, account, settings, notice=notice)
    if run.status in (RunStatus.READY, RunStatus.EXPIRED):
        return results.show(request, session, run, account, settings)
    context = {"account": account, "run": run, "label": LABELS[run.status], "progress": progress(run)}
    return page(request, "run.html", context, settings)


@router.get("/runs/{run_id}/status", response_model=None)
def run_status(run_id: uuid.UUID, *, session: DbSession, settings: AppSettings, account: SignedIn) -> Response:
    """What the run page polls while the run works."""
    run = visible_run(session, run_id, account, settings)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404, headers={"Cache-Control": "no-store"})
    found = progress(run)
    body = {
        "status": str(run.status),
        "label": LABELS[run.status],
        "detail": run.status_detail or "",
        "progress": asdict(found) if found else None,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
