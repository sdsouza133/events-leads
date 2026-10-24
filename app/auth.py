"""Passwordless sign-in (ADR 0004): one-time links, sessions and CSRF tokens.

A link's token and a session's cookie are random secrets, shown to their owner once and stored only as SHA-256
hashes, so a copy of the database signs nobody in. Neither is ever logged. The link's token is made by the worker
when it sends the email, so it never passes through the web process or the job queue at all.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, LoginToken, UserSession

LINK_LIFETIME = timedelta(minutes=30)
SESSION_COOKIE = "el_session"
CSRF_COOKIE = "el_csrf"
CSRF_FIELD = "csrf"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_link(session: Session, account_id: uuid.UUID, run_id: uuid.UUID | None, *, now: datetime) -> str:
    """A new sign-in token for the account, good once for LINK_LIFETIME. Returns the token; only its hash is kept."""
    token = new_secret()
    session.add(
        LoginToken(token_hash=digest(token), account_id=account_id, run_id=run_id, expires_at=now + LINK_LIFETIME)
    )
    return token


def usable_link(session: Session, token: str, *, now: datetime) -> LoginToken | None:
    """The link's row if it can still sign someone in, without using it (to show an expired link up front)."""
    row = session.scalar(select(LoginToken).where(LoginToken.token_hash == digest(token)))
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    return row


def redeem_link(session: Session, token: str, *, now: datetime) -> LoginToken | None:
    """Use the link: its row, marked used (row-locked, so two tabs can't both sign in with it), or None."""
    row = session.scalar(select(LoginToken).where(LoginToken.token_hash == digest(token)).with_for_update())
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    row.used_at = now
    return row


def open_session(session: Session, account_id: uuid.UUID, *, now: datetime, lifetime: timedelta) -> str:
    """A new signed-in session. Returns the cookie value; only its hash is kept."""
    token = new_secret()
    session.add(UserSession(token_hash=digest(token), account_id=account_id, expires_at=now + lifetime))
    return token


def signed_in(session: Session, cookie: str | None, *, now: datetime) -> Account | None:
    """The account behind a session cookie, if the session is current."""
    if not cookie:
        return None
    row = session.scalar(select(UserSession).where(UserSession.token_hash == digest(cookie)))
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    return session.get(Account, row.account_id)


def close_session(session: Session, cookie: str | None, *, now: datetime) -> None:
    if not cookie:
        return
    row = session.scalar(select(UserSession).where(UserSession.token_hash == digest(cookie)))
    if row is not None and row.revoked_at is None:
        row.revoked_at = now


def csrf_ok(cookie: str | None, field: str | None) -> bool:
    """Double-submit check: a form posted from another site can't carry our cookie's value in a field."""
    return bool(cookie) and bool(field) and hmac.compare_digest(str(cookie), str(field))
