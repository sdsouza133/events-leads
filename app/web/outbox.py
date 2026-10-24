"""The outbox: emails kept instead of sent where there's no Resend key (ADR 0004).

On a developer's machine (ENV=dev) the page is open: it's how you sign in when working locally. In previews
(ENV=staging, and the test suite) only operators, and the QA suite sending QA_TOKEN in an X-QA-Token header, can
read it. In production it doesn't exist, and production never keeps emails anyway.
"""

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select

from app.config import Settings
from app.models import Account, OutboxEmail
from app.web.common import AppSettings, DbSession, SignedIn, is_operator, page

router = APIRouter()
SHOWN = 25


@router.get("/dev/outbox", response_class=HTMLResponse)
def outbox_page(request: Request, *, session: DbSession, settings: AppSettings, account: SignedIn) -> HTMLResponse:
    if not _allowed(request, settings, account):
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    emails = session.scalars(select(OutboxEmail).order_by(OutboxEmail.id.desc()).limit(SHOWN)).all()
    return page(request, "outbox.html", {"account": account, "emails": emails}, settings)


@router.get("/dev/outbox/latest", response_model=None)
def latest_email(
    request: Request, *, session: DbSession, settings: AppSettings, account: SignedIn, to: str = ""
) -> Response:
    """The newest email to an address, as JSON: how the QA suite reads its sign-in link."""
    headers = {"Cache-Control": "no-store"}
    if not _allowed(request, settings, account):
        return JSONResponse({"error": "not found"}, status_code=404, headers=headers)
    email = session.scalars(
        select(OutboxEmail).where(OutboxEmail.to == to.strip().lower()).order_by(OutboxEmail.id.desc()).limit(1)
    ).first()
    if email is None:
        return JSONResponse({"error": "no email to that address"}, status_code=404, headers=headers)
    body = {"to": email.to, "subject": email.subject, "text": email.text, "sent_at": email.created_at.isoformat()}
    return JSONResponse(body, headers=headers)


def _allowed(request: Request, settings: Settings, account: Account | None) -> bool:
    if settings.env == "dev":
        return True
    if settings.env not in ("test", "staging"):
        return False
    if is_operator(account, settings):
        return True
    supplied = request.headers.get("x-qa-token", "")
    expected = settings.qa_token.get_secret_value() if settings.qa_token is not None else ""
    return bool(supplied) and bool(expected) and hmac.compare_digest(supplied, expected)
