"""What every page needs: templates, a database session, the signed-in account, and the CSRF cookie."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app import auth
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import Account, Run

TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def now() -> datetime:
    return datetime.now(UTC)


def db() -> Iterator[Session]:
    """A session for one request: committed if the handler returns, rolled back if it raises."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def current_account(request: Request, session: DbSession) -> Account | None:
    return auth.signed_in(session, request.cookies.get(auth.SESSION_COOKIE), now=now())


SignedIn = Annotated[Account | None, Depends(current_account)]


def is_operator(account: Account | None, settings: Settings) -> bool:
    return account is not None and account.email.lower() in settings.admin_emails


def visible_run(session: Session, run_id: uuid.UUID, account: Account | None, settings: Settings) -> Run | None:
    """The run, if this account may see it: its requester or an operator. Anyone else gets "not found", never
    "forbidden", so run ids can't be probed."""
    if account is None:
        return None
    run = session.get(Run, run_id)
    if run is None or not (run.account_id == account.id or is_operator(account, settings)):
        return None
    return run


async def posted_form(request: Request) -> FormData:
    """The whole posted form, for pages whose field names depend on what they show (one per event, say)."""
    return await request.form()


PostedForm = Annotated[FormData, Depends(posted_form)]


async def posted_json(request: Request) -> Any:
    """The request's JSON body, or None when it isn't JSON, for handlers that check who's asking first."""
    try:
        return await request.json()
    except ValueError:
        return None


PostedJson = Annotated[Any, Depends(posted_json)]


def secure_cookies(settings: Settings) -> bool:
    """Cookies are Secure wherever the site is served over https (everywhere but a developer's machine)."""
    return settings.base_url.startswith("https://")


def client_ip(request: Request) -> str:
    """The caller's address. Behind Railway's proxy, uvicorn takes it from X-Forwarded-For (--proxy-headers)."""
    return request.client.host if request.client else "unknown"


def page(
    request: Request,
    template: str,
    context: dict[str, Any],
    settings: Settings,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page. Every page carries the CSRF token its forms post back, set as a cookie the first time."""
    csrf = request.cookies.get(auth.CSRF_COOKIE) or auth.new_secret()
    shared = {"csrf": csrf, "csrf_field": auth.CSRF_FIELD, "operator": is_operator(context.get("account"), settings)}
    response = TEMPLATES.TemplateResponse(request, template, {**context, **shared}, status_code=status_code)
    if request.cookies.get(auth.CSRF_COOKIE) != csrf:
        response.set_cookie(
            auth.CSRF_COOKIE, csrf, httponly=True, secure=secure_cookies(settings), samesite="lax", path="/"
        )
    response.headers["Cache-Control"] = "no-store"  # pages show personal data and one-time links
    return response


def csrf_ok(request: Request, field: str) -> bool:
    return auth.csrf_ok(request.cookies.get(auth.CSRF_COOKIE), field)
