"""The emails the service sends, in one place. What goes into them is personal data: never log it (PRD)."""

import uuid

from app.providers.types import OutgoingEmail


def run_url(base_url: str, run_id: uuid.UUID) -> str:
    """The run's page (plan, progress, then results), for its requester once signed in."""
    return f"{base_url.rstrip('/')}/runs/{run_id}"


def results_email(
    *, to: str, base_url: str, run_id: uuid.UUID, leads: int, events: list[str], days: int
) -> OutgoingEmail:
    """The link to a finished run. Its key makes Resend deliver it once, however often a retry sends it."""
    link = run_url(base_url, run_id)
    where = _together(events) or "your events"
    if leads:
        people = "1 person" if leads == 1 else f"{leads} people"
        subject = "Your event leads are ready"
        body = (
            f"We found {people} who went to, or are going to, {where}, who match your ICP and have a verified work "
            f"email.\n\nSee the list and download it as a CSV:\n{link}\n\nThe list is available for {days} days."
        )
    else:
        subject = "Your event leads search has finished"
        body = (
            f"We read the LinkedIn posts about {where}, but found nobody who matches your ICP and has a verified "
            f"work email.\n\nThe details are here:\n{link}"
        )
    return OutgoingEmail(to=to, subject=subject, text=f"Hi,\n\n{body}\n\nEvent Leads\n", key=f"results:{run_id}")


def _together(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
