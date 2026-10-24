"""A finished run's list: the cards on its page and the CSV download (issue #8, FR-8).

The list is the leads enrich included, best first: the highest ICP score, then the most telling post (a speaker
before an attendee), then by name. The page shows each lead as a card, and can show only the speakers, exhibitors,
people going or people who went. It stays available for RESULTS_DAYS after the run finished. After that the page
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
TONES = {"Speaking": "warm", "Exhibiting": "accent", "Going": "ok", "Went": "neutral"}
MATCHES = {
    "title": "title",
    "headline": "title",
    "seniority": "seniority",
    "industry": "industry",
    "company": "company size",
}
"""What each reason the ICP score gives is about, by its first word. A headline stands in for a missing title."""
COLUMNS = ["Name", "Title", "Company", "Email", "LinkedIn", "Event", "Attendance", "Post", "ICP score", "Why they fit"]
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")
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

    @property
    def initials(self) -> str:
        words = self.name.split()
        return "".join(word[0] for word in words[:1] + words[1:][-1:]).upper()

    @property
    def tone(self) -> str:
        return TONES.get(self.attendance, "neutral")

    @property
    def matches(self) -> str:
        """Why they fit, in words: 'Matches on title, seniority and industry'."""
        about = dict.fromkeys(MATCHES.get(reason.split(" ", 1)[0], "") for reason in self.reasons)
        named = [name for name in about if name]
        return f"Matches on {emails.together(named)}" if named else ""


@dataclass(frozen=True)
class Filter:
    label: str
    count: int
    href: str
    current: bool


def show(
    request: Request,
    session: Session,
    run: Run,
    account: Account,
    settings: Settings,
    *,
    only: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The run's page once it has finished: its list (all of it, or only one kind of attendee), or that it has
    expired."""
    now = common.now()
    found = [] if expired(run, now) else rows(session, run)
    events = list(dict.fromkeys(row.event for row in found))
    showing = only.capitalize() if only.capitalize() in TONES else ""
    context = {
        "account": account,
        "run": run,
        "rows": found,
        "shown": [row for row in found if not showing or row.attendance == showing],
        "filters": _filters(run, found, showing),
        "events": emails.together(events),
        "expired": expired(run, now),
        "until": _short_day(run.results_expire_at),
        "days": settings.results_days,
    }
    return page(request, "results.html", context, settings, status_code=status_code)


def _filters(run: Run, found: list[Row], showing: str) -> list[Filter]:
    """All, then each kind of attendee the list has, with how many."""
    base = f"/runs/{run.id}"
    kinds = [kind for kind in TONES if any(row.attendance == kind for row in found)]
    return [Filter("All", len(found), f"{base}#leads", not showing)] + [
        Filter(
            kind,
            sum(1 for row in found if row.attendance == kind),
            f"{base}?only={kind.lower()}#leads",
            kind == showing,
        )
        for kind in kinds
    ]


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


def _short_day(value: datetime | None) -> str:
    return f"{value.day} {value:%b}" if value else ""
