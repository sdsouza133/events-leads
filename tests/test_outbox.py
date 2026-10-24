"""The outbox that stands in for Resend where there's no key (ADR 0004)."""

import dataclasses

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEmail
from app.providers.outbox import OutboxMailer
from app.providers.types import OutgoingEmail

pytestmark = pytest.mark.db


def test_each_key_is_kept_once_like_resend_delivers_it_once(db: Session) -> None:
    email = OutgoingEmail(to="jo@acmewidgets.com", subject="Your list", text="first", key="results:1")
    mailer = OutboxMailer()
    mailer.send(email)
    mailer.send(dataclasses.replace(email, text="second"))  # a job retried after a crash
    mailer.send(dataclasses.replace(email, key="results:2"))
    db.expire_all()
    kept = [(row.key, row.to, row.text) for row in db.scalars(select(OutboxEmail).order_by(OutboxEmail.id))]
    assert kept == [("results:1", "jo@acmewidgets.com", "first"), ("results:2", "jo@acmewidgets.com", "first")]
