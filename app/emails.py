"""The emails the service sends, in one place. What goes into them is personal data: never log it (PRD)."""

import uuid

from app.auth import digest
from app.providers.types import OutgoingEmail


def run_url(base_url: str, run_id: uuid.UUID) -> str:
    """The run's page (plan, progress, then results), for its requester once signed in."""
    return f"{base_url.rstrip('/')}/runs/{run_id}"


def sign_in_email(*, to: str, base_url: str, token: str, domain: str | None, minutes: int) -> OutgoingEmail:
    """The one-time sign-in link (ADR 0004). Its key comes from the token, so a retry that makes a new link sends a
    new email rather than being refused as a different email with the same key."""
    link = f"{base_url.rstrip('/')}/sign-in?token={token}"
    what = f"your free list for {domain}" if domain else "Event Leads"
    body = (
        f"Here's your link to {what}:\n{link}\n\nIt works once, for {minutes} minutes. "
        "Didn't ask for it? Just ignore this email."
    )
    return OutgoingEmail(
        to=to,
        subject="Your sign-in link for Event Leads",
        text=f"Hi,\n\n{body}\n\nEvent Leads\n",
        key=f"sign-in:{digest(token)[:32]}",
    )


def results_email(
    *, to: str, base_url: str, run_id: uuid.UUID, leads: int, events: list[str], days: int
) -> OutgoingEmail:
    """The link to a finished run. Its key makes Resend deliver it once, however often a retry sends it."""
    link = run_url(base_url, run_id)
    where = together(events) or "your events"
    if leads:
        people = "1 lead" if leads == 1 else f"{leads} leads"
        subject = f"Your list is ready: {people}"
        body = (
            f"Your list is ready: {people} from {where}. Each one is going to "
            f"{'it' if len(events) == 1 else 'one of these events'} or went, matches your buyers, and has a "
            f"verified work email.\n\n"
            f"See it and download the CSV:\n{link}\n\nIt's yours for {days} days."
        )
    else:
        subject = "Your Event Leads search is done"
        body = (
            f"We read the LinkedIn posts about {where}, but didn't find anyone there who matches your buyers and "
            f"has a verified work email.\n\nThe details are here:\n{link}"
        )
    return OutgoingEmail(to=to, subject=subject, text=f"Hi,\n\n{body}\n\nEvent Leads\n", key=f"results:{run_id}")


def together(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
