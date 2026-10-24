"""Small builders for test data. Synthetic values only."""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Account, Run


def make_account(session: Session, *, domain: str = "acme.example") -> Account:
    account = Account(email=f"user-{uuid.uuid4().hex[:8]}@{domain}", domain=domain)
    session.add(account)
    session.flush()
    return account


def make_run(session: Session, *, budget_usd: Decimal = Decimal("3.00"), domain: str = "acme.example") -> Run:
    account = make_account(session, domain=domain)
    run = Run(account_id=account.id, domain=domain, website_url=f"https://{domain}/", budget_usd=budget_usd)
    session.add(run)
    session.flush()
    return run
