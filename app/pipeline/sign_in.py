"""Email a requester their sign-in link (ADR 0004).

The web process only queues this job. The worker makes the link's token here, stores its hash, and sends the
email, so the token itself never passes through the web process, the job queue or the logs. A retry after a failed
send makes a new link; the one that never arrived simply expires.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app import auth, emails
from app.models import Account, Run
from app.pipeline.base import DONE, JobContext, Outcome, Payload

log = logging.getLogger(__name__)

SEND_SIGN_IN = "send_sign_in"


def send_sign_in(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    account = session.get(Account, uuid.UUID(payload["account_id"]))
    if account is None:
        return DONE
    run_id = uuid.UUID(payload["run_id"]) if payload.get("run_id") else None
    run = session.get(Run, run_id) if run_id is not None else None
    token = auth.issue_link(session, account.id, run_id, now=ctx.now())
    message = emails.sign_in_email(
        to=account.email,
        base_url=ctx.settings.base_url,
        token=token,
        domain=run.domain if run is not None else None,
        minutes=int(auth.LINK_LIFETIME.total_seconds() // 60),
    )
    session.commit()  # the link has to work before it arrives; nothing is held open while the email goes out
    ctx.providers.mailer.send(message)
    log.info("sign-in link sent", extra={"account_id": str(account.id)})
    return DONE
