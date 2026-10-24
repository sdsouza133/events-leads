"""The operator console (issue #9, FR-9): every run with its status and cost, the day's spend against the cap, how
the job queue is doing, and the operator's actions: running any company, pausing new runs, canceling a run, and
granting a company another run.

Running a company needs no prospect's email: the run is the operator's own, started at once, with the same caps as
any run. It doesn't use the company's free run, so the company can still ask for its own list.

Only the addresses in ADMIN_EMAILS see it. To anyone else signed in it doesn't exist (404), like a run that isn't
theirs. Operators sign in like requesters: the Sign in link (/sign-in-again) brings them here.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import budget, domains, flags, queue
from app.config import Settings
from app.models import Account, AiTask, BudgetDay, Job, JobStatus, Run, RunGrant, RunStatus
from app.pipeline import runs
from app.pipeline.analyze import ANALYZE_SITE
from app.web import common, start
from app.web.common import AppSettings, DbSession, SignedIn, page

log = logging.getLogger(__name__)
router = APIRouter()

RUNS_SHOWN = 100
FAILED_JOBS_SHOWN = 20
GRANTS_SHOWN = 20
MAX_NOTE_LENGTH = 300
FINISHED = frozenset({RunStatus.READY, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED})
CANCELED = "We stopped this run before it finished."
NOTICES = {
    "canceled": "The run is canceled. Nothing new starts for it.",
    "finished": "That run had already finished, so it wasn't canceled.",
    "granted": "Granted: the company can start another run.",
    "paused": "New runs are paused: the start form takes no new requests. Runs already asked for carry on.",
    "opened": "The start form takes new runs again.",
}
STATUSES = {  # how the console names each status, and the color of its chip
    RunStatus.AWAITING_SIGN_IN: ("Awaiting sign-in", "neutral"),
    RunStatus.ANALYZING: ("Making the plan", "accent"),
    RunStatus.AWAITING_APPROVAL: ("Plan ready", "accent"),
    RunStatus.COLLECTING: ("Searching", "warm"),
    RunStatus.WAITING_FOR_BUDGET: ("Waiting for budget", "warm"),
    RunStatus.QUALIFYING: ("Reading posts", "warm"),
    RunStatus.ENRICHING: ("Finding emails", "warm"),
    RunStatus.READY: ("Ready", "ok"),
    RunStatus.FAILED: ("Failed", "error"),
    RunStatus.CANCELED: ("Canceled", "neutral"),
    RunStatus.EXPIRED: ("Expired", "neutral"),
}
EXPIRED_FORM = "This page had expired. Please try again."


@dataclass(frozen=True)
class Today:
    day: str
    spent: str
    reserved: str
    cap: str
    left: str
    percent: int  # of the cap, spent or held
    runs: int  # started today


@dataclass(frozen=True)
class Queue:
    due: int
    later: int
    running: int
    failed: int


@dataclass(frozen=True)
class FailedJob:
    when: str
    kind: str
    run_id: str | None
    attempts: int
    error: str


@dataclass(frozen=True)
class RunRow:
    id: uuid.UUID
    created: str
    domain: str
    requester: str
    status: str
    tone: str
    detail: str
    error: str
    spent: str
    credits: int
    leads: int
    open: bool
    operator_run: bool


@dataclass(frozen=True)
class Grant:
    when: str
    domain: str
    by: str
    note: str


@router.get("/admin", response_class=HTMLResponse)
def console(
    request: Request, *, session: DbSession, settings: AppSettings, account: SignedIn, notice: str = ""
) -> HTMLResponse:
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    return _render(request, session, account, settings, notice=NOTICES.get(notice, ""))


@router.post("/admin/runs", response_model=None)
def run_any_company(
    request: Request,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    website: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """A run for any company's website, started at once. It's the operator's own: they review its plan, and the
    list comes to them."""
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    errors: dict[str, str] = {}
    site = domains.website(website)
    if not common.csrf_ok(request, csrf):
        errors["form"] = EXPIRED_FORM
    elif site is None:
        errors["website"] = "Enter the company's website, for example acme.com."
    if errors or site is None:
        form = {"website": website}
        return _render(request, session, account, settings, errors=errors, form=form, status_code=400)
    url, domain = site
    run = Run(
        account_id=account.id,
        domain=domain,
        website_url=url,
        budget_usd=settings.run_budget_usd,
        status=RunStatus.ANALYZING,
        status_detail=start.READING_SITE,
        operator_run=True,
    )
    session.add(run)
    session.flush()
    now = common.now()
    queue.enqueue(session, ANALYZE_SITE, {"run_id": str(run.id)}, run_after=now, dedupe_key=f"{ANALYZE_SITE}:{run.id}")
    session.commit()  # before the redirect: the session's own commit comes after the response is sent
    log.info("operator run started", extra={"run_id": str(run.id), "account_id": str(account.id)})
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/admin/new-runs", response_model=None)
def new_runs(
    request: Request,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    take: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """The console's switch: whether the start form takes new runs ("yes" or "no")."""
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    if not common.csrf_ok(request, csrf) or take not in ("yes", "no"):
        return _render(request, session, account, settings, errors={"form": EXPIRED_FORM}, status_code=400)
    pause = take == "no"
    flags.turn(session, flags.NEW_RUNS_PAUSED, on=pause, by=account.id, now=common.now())
    session.commit()
    log.info("new runs paused" if pause else "new runs opened", extra={"account_id": str(account.id)})
    return RedirectResponse(f"/admin?notice={'paused' if pause else 'opened'}", status_code=303)


@router.get("/admin/runs/{run_id}/cancel", response_class=HTMLResponse)
def cancel_page(
    request: Request, run_id: uuid.UUID, *, session: DbSession, settings: AppSettings, account: SignedIn
) -> HTMLResponse:
    """Canceling can't be undone, so the console's link asks first, here."""
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    run = session.get(Run, run_id)
    if run is None:
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    return _cancel_page(request, run, account, settings)


@router.post("/admin/runs/{run_id}/cancel", response_model=None)
def cancel(
    request: Request,
    run_id: uuid.UUID,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    run = runs.locked(session, run_id)
    if run is None:
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    if not common.csrf_ok(request, csrf):
        return _cancel_page(request, run, account, settings, error=EXPIRED_FORM, status_code=400)
    if run.status in FINISHED:
        notice = "finished"
    else:
        # Every step checks the run's status before it starts anything, so this is all it takes. A call already
        # under way finishes and its cost is counted.
        run.status, run.status_detail, run.completed_at = RunStatus.CANCELED, CANCELED, common.now()
        run.error = "; ".join(filter(None, [run.error, "canceled by an operator"]))
        notice = "canceled"
        log.info("run canceled", extra={"run_id": str(run.id), "account_id": str(account.id)})
    session.commit()  # before the redirect: the session's own commit comes after the response is sent
    return RedirectResponse(f"/admin?notice={notice}#runs", status_code=303)


@router.post("/admin/grants", response_model=None)
def grant(
    request: Request,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    company: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """One more run for a company, on top of its free ones. Each grant is good for one run."""
    refusal = _refusal(request, account, settings)
    if refusal is not None:
        return refusal
    assert account is not None  # noqa: S101 (_refusal turned away anyone signed out)
    form = {"company": company, "note": note}
    errors: dict[str, str] = {}
    site = domains.website(company)
    if not common.csrf_ok(request, csrf):
        errors["form"] = EXPIRED_FORM
    elif site is None:
        errors["company"] = "Enter the company's website or domain, for example acme.com."
    elif len(note.strip()) > MAX_NOTE_LENGTH:
        errors["note"] = f"Keep the note to {MAX_NOTE_LENGTH} characters."
    if errors or site is None:
        return _render(request, session, account, settings, errors=errors, form=form, status_code=400)
    session.add(RunGrant(domain=site[1], granted_by=account.id, note=note.strip() or None))
    session.commit()
    log.info("run granted", extra={"domain": site[1], "account_id": str(account.id)})
    return RedirectResponse("/admin?notice=granted#grants", status_code=303)


def _refusal(request: Request, account: Account | None, settings: Settings) -> HTMLResponse | None:
    """The page for anyone but an operator (sign in, or, signed in as someone else, not found); None for one."""
    if account is None:
        return page(request, "sign_in_needed.html", {"account": None}, settings, status_code=401)
    if not common.is_operator(account, settings):
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    return None


def _cancel_page(
    request: Request, run: Run, account: Account, settings: Settings, *, error: str = "", status_code: int = 200
) -> HTMLResponse:
    context = {"account": account, "run": run, "finished": run.status in FINISHED, "error": error}
    return page(request, "cancel_run.html", context, settings, status_code=status_code)


def _render(
    request: Request,
    session: Session,
    account: Account,
    settings: Settings,
    *,
    notice: str = "",
    errors: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    now = common.now()
    context: dict[str, Any] = {
        "account": account,
        "paused": settings.pause_new_runs,
        "taking": not flags.is_on(session, flags.NEW_RUNS_PAUSED),
        "budget": _usd(settings.run_budget_usd),
        "manual_ai": settings.manual_ai,
        "operator_api": settings.operator_api_token is not None,
        "questions": session.scalar(select(func.count()).select_from(AiTask).where(AiTask.answer.is_(None))) or 0,
        "free_runs": settings.free_runs_per_domain,
        "notice": notice,
        "errors": errors or {},
        "form": form or {},
        "today": _today(session, settings, now),
        "queue": _queue(session, now),
        "failed_jobs": _failed_jobs(session),
        "runs": _runs(session, account),
        "grants": _grants(session),
    }
    return page(request, "console.html", context, settings, status_code=status_code)


def _today(session: Session, settings: Settings, now: datetime) -> Today:
    day = budget.utc_day(now)
    row = session.get(BudgetDay, day)
    spent, reserved = (row.spent_usd, row.reserved_usd) if row is not None else (Decimal(0), Decimal(0))
    left = budget.remaining_today(session, settings.daily_cap_usd, now=now)
    used = min(100, round((spent + reserved) * 100 / settings.daily_cap_usd))
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    started = session.scalar(select(func.count()).select_from(Run).where(Run.created_at >= midnight)) or 0
    return Today(day.isoformat(), _usd(spent), _usd(reserved), _usd(settings.daily_cap_usd), _usd(left), used, started)


def _queue(session: Session, now: datetime) -> Queue:
    counts = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    due = session.scalar(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
    )
    queued = counts.get(JobStatus.QUEUED, 0)
    return Queue(
        due=due or 0,
        later=queued - (due or 0),
        running=counts.get(JobStatus.RUNNING, 0),
        failed=counts.get(JobStatus.FAILED, 0),
    )


def _failed_jobs(session: Session) -> list[FailedJob]:
    jobs = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.FAILED)
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(FAILED_JOBS_SHOWN)
    )
    return [
        FailedJob(_when(job.updated_at), job.kind, _run_id(job.payload), job.attempts, job.last_error or "")
        for job in jobs
    ]


def _runs(session: Session, viewer: Account) -> list[RunRow]:
    rows = session.execute(
        select(Run, Account.email)
        .join(Account, Account.id == Run.account_id)
        .order_by(Run.created_at.desc(), Run.id)
        .limit(RUNS_SHOWN)
    )
    return [
        RunRow(
            id=run.id,
            created=_when(run.created_at),
            domain=run.domain,
            requester="You" if email == viewer.email else email,
            status=STATUSES[run.status][0],
            tone=STATUSES[run.status][1],
            detail=run.status_detail or "",
            error=run.error or "",
            spent=f"{_usd(run.spent_usd)} of {_usd(run.budget_usd)}"
            + (f", {_usd(run.reserved_usd)} held" if run.reserved_usd else ""),
            credits=run.apollo_credits_used,
            leads=run.leads_count,
            open=run.status not in FINISHED,
            operator_run=run.operator_run,
        )
        for run, email in rows
    ]


def _grants(session: Session) -> list[Grant]:
    rows = session.execute(
        select(RunGrant, Account.email)
        .outerjoin(Account, Account.id == RunGrant.granted_by)
        .order_by(RunGrant.created_at.desc(), RunGrant.id.desc())
        .limit(GRANTS_SHOWN)
    )
    return [Grant(_when(grant.created_at), grant.domain, email or "", grant.note or "") for grant, email in rows]


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _run_id(payload: dict[str, Any]) -> str | None:
    """The run a job was for, when its payload names one we can link to."""
    try:
        return str(uuid.UUID(str(payload["run_id"])))
    except (KeyError, ValueError):
        return None
