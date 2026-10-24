"""A finished run's list: the table on its page and the CSV download (issue #8, FR-8).

The list is the leads enrich included, best first: the highest ICP score, then the most telling post (a speaker
before an attendee), then by name. It stays available for RESULTS_DAYS after the run finished. After that the page
says so, the CSV is gone, and the retention job deletes the data.
"""

import csv
import io
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import emails
from app.config import Settings
from app.models import Account, Event, Lead, Person, Post, Run, RunStatus
from app.schemas import MOST_TELLING
from app.web import common
from app.web.common import AppSettings, DbSession, SignedIn, page

log = logging.getLogger(__name__)
router = APIRouter()

ATTENDANCE = {"speaking": "Speaking", "exhibiting": "Exhibiting", "attending": "Going", "attended": "Went"}
COLUMNS = ["Name", "Title", "Company", "Email", "LinkedIn", "Event", "Attendance", "Post", "ICP score", "Why they fit"]
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
"""A cell starting with one of these is run as a formula when the CSV is opened in a spreadsheet (CSV injection)."""


@dataclass(frozen=True)
class Row:
    name: str
    title: str
    company: str
    email: str
    linkedin_url: str
    event: str
    attendance: str
    post_url: str
    score: int
    reasons: list[str]


def show(
    request: Request, session: Session, run: Run, account: Account, settings: Settings, *, status_code: int = 200
) -> HTMLResponse:
    """The run's page once it has finished: its list, or that it has expired."""
    now = common.now()
    found = [] if expired(run, now) else rows(session, run)
    events = list(dict.fromkeys(row.event for row in found))
    context = {
        "account": account,
        "run": run,
        "rows": found,
        "events": emails.together(events),
        "expired": expired(run, now),
        "until": _day(run.results_expire_at),
        "days": settings.results_days,
    }
    return page(request, "results.html", context, settings, status_code=status_code)


@router.get("/runs/{run_id}/leads.csv", response_model=None)
def download(
    request: Request, run_id: uuid.UUID, *, session: DbSession, settings: AppSettings, account: SignedIn
) -> Response:
    if account is None:
        return page(request, "sign_in_needed.html", {"account": None}, settings, status_code=401)
    run = common.visible_run(session, run_id, account, settings)
    if run is None or run.status not in (RunStatus.READY, RunStatus.EXPIRED):
        return page(request, "not_found.html", {"account": account}, settings, status_code=404)
    if expired(run, common.now()):
        return show(request, session, run, account, settings, status_code=410)
    log.info("results downloaded", extra={"run_id": str(run.id), "account_id": str(account.id)})
    return Response(
        "\ufeff" + to_csv(rows(session, run)),  # the byte-order mark tells Excel the file is UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_filename(run)}"', "Cache-Control": "no-store"},
    )


def expired(run: Run, now: datetime) -> bool:
    return run.status == RunStatus.EXPIRED or (run.results_expire_at is not None and run.results_expire_at <= now)


def rows(session: Session, run: Run) -> list[Row]:
    found = session.execute(
        select(Lead, Person, Event.name, Post.url)
        .join(Person, Person.id == Lead.person_id)
        .join(Event, Event.id == Lead.event_id)
        .join(Post, Post.id == Lead.post_id)
        .where(Lead.run_id == run.id, Lead.included.is_(True))
    )
    listed = [
        Row(
            name=person.name or "",
            title=person.title or "",
            company=person.company_name or "",
            email=person.email or "",
            linkedin_url=_web(person.linkedin_url),
            event=event,
            attendance=ATTENDANCE.get(lead.attendance, lead.attendance),
            post_url=_web(post_url),
            score=lead.icp_score,
            reasons=[str(reason) for reason in lead.icp_reasons],
        )
        for lead, person, event, post_url in found
    ]
    telling = {ATTENDANCE[str(label)]: rank for rank, label in enumerate(MOST_TELLING)}
    return sorted(listed, key=lambda row: (-row.score, telling.get(row.attendance, len(telling)), row.name.casefold()))


def to_csv(listed: list[Row]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for row in listed:
        writer.writerow(
            _cell(value)
            for value in (
                row.name,
                row.title,
                row.company,
                row.email,
                row.linkedin_url,
                row.event,
                row.attendance,
                row.post_url,
                str(row.score),
                "; ".join(row.reasons),
            )
        )
    return out.getvalue()


def _cell(value: str) -> str:
    """A cell a spreadsheet shows as text: one that would start a formula gets a leading apostrophe."""
    return f"'{value}" if value.startswith(FORMULA_STARTS) else value


def _web(url: str | None) -> str:
    """The address if it's a web page, so a link can't run script ('javascript:...')."""
    return url if url and url.lower().startswith(("https://", "http://")) else ""


def _filename(run: Run) -> str:
    when = f"-{run.completed_at:%Y-%m-%d}" if run.completed_at else ""
    return f"leads-{re.sub(r'[^A-Za-z0-9.-]', '-', run.domain)}{when}.csv"


def _day(value: datetime | None) -> str:
    return f"{value.day} {value:%B %Y}" if value else ""
