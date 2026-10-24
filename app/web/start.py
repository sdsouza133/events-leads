"""Starting a run: the form, the emailed sign-in link, and signing out (issue #8, ADR 0004).

The form takes a website and a work email on the same company domain. Nothing is spent until the requester signs
in from the link we email: only then does the run start reading their website, and only if the company hasn't used
its free run. Sign-in itself is a POST from a page the link opens, so the mail scanners that open links on their
own can't use up a one-time link.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import auth, domains, queue, ratelimit
from app.config import Settings
from app.logging_config import mask_email
from app.models import Account, Run, RunStatus
from app.pipeline import runs
from app.pipeline.analyze import ANALYZE_SITE
from app.pipeline.sign_in import SEND_SIGN_IN
from app.web import common
from app.web.common import AppSettings, DbSession, SignedIn, page

log = logging.getLogger(__name__)
router = APIRouter()

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_EMAIL_LENGTH = 320
HOUR = timedelta(hours=1)

READING_SITE = "Reading your website"
FREE_RUN_USED = "Your company has already used its free run."


@router.get("/", response_class=HTMLResponse)
def start_page(request: Request, *, account: SignedIn, settings: AppSettings) -> HTMLResponse:
    return page(request, "start.html", {"account": account, "form": {}, "errors": {}}, settings)


@router.post("/start", response_class=HTMLResponse)
def start(
    request: Request,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    website: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> HTMLResponse:
    form = {"website": website, "email": email}

    def again(errors: dict[str, str], status_code: int) -> HTMLResponse:
        context = {"account": account, "form": form, "errors": errors}
        return page(request, "start.html", context, settings, status_code=status_code)

    if not common.csrf_ok(request, csrf):
        return again({"form": "This page had expired. Please send the form again."}, 400)
    site = domains.website(website)
    address = email.strip().lower()
    errors = _check(site, address)
    if errors:
        return again(errors, 400)
    if not _within_limits(session, settings, request, address):
        session.commit()  # the attempt still counts
        log.warning("sign-up attempts over the limit", extra={"email": mask_email(address)})
        return again({"form": "Too many attempts. Please try again in an hour."}, 429)
    assert site is not None  # noqa: S101 (_check found no error, so the website parsed)
    url, domain = site
    if runs.runs_used(session, domain) >= settings.free_runs_per_domain:
        session.commit()
        return page(request, "used.html", {"account": account, "domain": domain}, settings)

    requester = _account(session, address, domain)
    run = _pending_run(session, requester.id, domain) or Run(
        account_id=requester.id, domain=domain, budget_usd=settings.run_budget_usd, status=RunStatus.AWAITING_SIGN_IN
    )
    run.website_url = url
    session.add(run)
    session.flush()
    payload = {"account_id": str(requester.id), "run_id": str(run.id)}
    queue.enqueue(session, SEND_SIGN_IN, payload, run_after=common.now(), dedupe_key=f"{SEND_SIGN_IN}:{run.id}")
    session.commit()
    log.info("sign-in link requested", extra={"email": mask_email(address), "run_id": str(run.id)})
    return page(request, "check_email.html", {"account": account, "email": address}, settings)


@router.get("/sign-in", response_class=HTMLResponse)
def sign_in_page(
    request: Request, *, session: DbSession, settings: AppSettings, account: SignedIn, token: str = ""
) -> HTMLResponse:
    """The page the emailed link opens. Checking the link here doesn't use it: the button below does."""
    usable = bool(token) and auth.usable_link(session, token, now=common.now()) is not None
    context = {"account": account, "token": token if usable else "", "expired": not usable}
    return page(request, "sign_in.html", context, settings, status_code=200 if usable else 400)


@router.post("/sign-in", response_model=None)
def sign_in(
    request: Request,
    *,
    session: DbSession,
    settings: AppSettings,
    account: SignedIn,
    token: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    if not common.csrf_ok(request, csrf):
        context = {"account": account, "token": token, "expired": False, "error": "This page had expired."}
        return page(request, "sign_in.html", context, settings, status_code=400)
    now = common.now()
    link = auth.redeem_link(session, token, now=now) if token else None
    if link is None:
        return page(
            request, "sign_in.html", {"account": account, "token": "", "expired": True}, settings, status_code=400
        )
    requester = session.get(Account, link.account_id)
    if requester is None:  # deleted since the link was sent
        return page(request, "sign_in.html", {"account": None, "token": "", "expired": True}, settings, status_code=400)
    requester.verified_at = requester.verified_at or now
    requester.last_sign_in_at = now
    cookie = auth.open_session(session, requester.id, now=now, lifetime=timedelta(days=settings.session_days))
    if link.run_id is not None:
        _begin(session, settings, link.run_id, now=now)
    session.commit()
    log.info("signed in", extra={"account_id": str(requester.id)})
    response = RedirectResponse(f"/runs/{link.run_id}" if link.run_id else "/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        cookie,
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        secure=common.secure_cookies(settings),
        samesite="lax",
        path="/",
    )
    response.delete_cookie(auth.CSRF_COOKIE, path="/")  # a fresh one comes with the next page
    return response


@router.post("/sign-out", response_model=None)
def sign_out(
    request: Request, *, session: DbSession, settings: AppSettings, csrf: Annotated[str, Form()] = ""
) -> Response:
    if common.csrf_ok(request, csrf):
        auth.close_session(session, request.cookies.get(auth.SESSION_COOKIE), now=common.now())
        session.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/", secure=common.secure_cookies(settings), httponly=True)
    return response


def _check(site: tuple[str, str] | None, address: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if site is None:
        errors["website"] = "Enter your company's website, like acme.com."
    mailbox = domains.email_domain(address) if len(address) <= MAX_EMAIL_LENGTH and EMAIL.fullmatch(address) else None
    if mailbox is None:
        errors["email"] = "Enter your work email address."
    elif domains.is_free_mail(mailbox):
        errors["email"] = "Use your work email. A personal address, like Gmail, doesn't show where you work."
    elif site is not None and mailbox != site[1]:
        errors["email"] = f"Use an email address at {site[1]}, your website's domain."
    return errors


def _within_limits(session: Session, settings: Settings, request: Request, address: str) -> bool:
    """SIGN_UPS_PER_HOUR from one IP address, and to one email address (so nobody can flood someone's inbox).
    The address is kept only as a hash in the counter's key."""
    limit = settings.sign_ups_per_hour
    by_ip = ratelimit.hit(session, f"sign-up:ip:{common.client_ip(request)}", limit=limit, window=HOUR)
    by_email = ratelimit.hit(session, f"sign-up:email:{auth.digest(address)[:40]}", limit=limit, window=HOUR)
    return by_ip and by_email


def _account(session: Session, address: str, domain: str) -> Account:
    session.execute(insert(Account).values(id=uuid.uuid4(), email=address, domain=domain).on_conflict_do_nothing())
    return session.scalars(select(Account).where(Account.email == address)).one()


def _pending_run(session: Session, account_id: uuid.UUID, domain: str) -> Run | None:
    """The run this requester asked for and hasn't signed in to yet: asking again resends its link."""
    return session.scalars(
        select(Run)
        .where(Run.account_id == account_id, Run.domain == domain, Run.status == RunStatus.AWAITING_SIGN_IN)
        .order_by(Run.created_at.desc())
        .limit(1)
    ).first()


def _begin(session: Session, settings: Settings, run_id: uuid.UUID, *, now: datetime) -> None:
    """Start the run the link was for: read the website, unless the company has used its free runs since."""
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.AWAITING_SIGN_IN:
        return  # already started (an earlier link), or stopped
    runs.lock_domain(session, run.domain)
    if runs.runs_used(session, run.domain) >= settings.free_runs_per_domain:
        run.status, run.status_detail, run.completed_at = RunStatus.CANCELED, FREE_RUN_USED, now
        return
    run.status, run.status_detail = RunStatus.ANALYZING, READING_SITE
    queue.enqueue(session, ANALYZE_SITE, {"run_id": str(run.id)}, run_after=now, dedupe_key=f"{ANALYZE_SITE}:{run.id}")
