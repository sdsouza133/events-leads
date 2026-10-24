"""A run's page: where it stands, for its requester (or an operator) only (issue #8).

While the run awaits approval, the page is its plan (`app/web/plan.py`); while it works, it shows its progress;
once it has finished, it is the list (`app/web/results.py`).
"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.models import RunStatus
from app.web import plan, results
from app.web.common import AppSettings, DbSession, SignedIn, page, visible_run

router = APIRouter()

LABELS = {
    RunStatus.AWAITING_SIGN_IN: "Waiting for you to sign in",
    RunStatus.ANALYZING: "Reading your website",
    RunStatus.AWAITING_APPROVAL: "Your plan is ready",
    RunStatus.COLLECTING: "Finding posts about your events",
    RunStatus.WAITING_FOR_BUDGET: "Waiting for capacity",
    RunStatus.QUALIFYING: "Reading the posts",
    RunStatus.ENRICHING: "Finding work emails",
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
    context = {"account": account, "run": run, "label": LABELS[run.status], "working": run.status in WORKING}
    return page(request, "run.html", context, settings)


@router.get("/runs/{run_id}/status", response_model=None)
def run_status(run_id: uuid.UUID, *, session: DbSession, settings: AppSettings, account: SignedIn) -> Response:
    """What the run page polls while the run works."""
    run = visible_run(session, run_id, account, settings)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404, headers={"Cache-Control": "no-store"})
    body = {"status": str(run.status), "label": LABELS[run.status], "detail": run.status_detail or ""}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
