"""A working run's page shows how far it has got as a few steps (issue #8): the plan's two while it's made, the
search's three once it's approved."""

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.main import create_app
from app.models import Event, EventPost, Lead, Person, Post, PostLabel, Run, RunEvent, RunEventStatus, RunStatus
from app.pipeline import analyze, collect, qualify
from app.web import run_page, start
from tests.factories import make_run

PEOPLE = "https://www.linkedin.com/in/someone-"
_ids = itertools.count(7_300_000_000_000_000_000)


def working(status: RunStatus, detail: str, *, icp: dict[str, object] | None = None) -> Run:
    return Run(domain="acmewidgets.com", status=status, status_detail=detail, icp=icp)


def test_the_plan_starts_by_reading_the_website() -> None:
    found = run_page.progress(working(RunStatus.ANALYZING, start.READING_SITE))
    assert found == run_page.Progress(
        "plan", ["Reading acmewidgets.com", "Finding events your buyers go to"], 0, note=""
    )


def test_once_the_website_is_read_the_events_are_next() -> None:
    found = run_page.progress(working(RunStatus.ANALYZING, analyze.FINDING_EVENTS, icp={"company_name": "Acme"}))
    assert found is not None
    assert (found.phase, found.step, found.note) == ("plan", 1, "")


@pytest.mark.parametrize(
    ("status", "detail", "step"),
    [
        (RunStatus.COLLECTING, collect.SEARCHING, 0),
        (RunStatus.QUALIFYING, collect.READING_POSTS, 1),
        (RunStatus.ENRICHING, qualify.ENRICHING, 2),
    ],
)
def test_the_search_has_three_steps(status: RunStatus, detail: str, step: int) -> None:
    found = run_page.progress(working(status, detail))
    assert found is not None
    assert (found.phase, found.steps, found.step, found.note) == ("search", run_page.SEARCH_STEPS, step, "")


def test_waiting_for_capacity_is_said_in_a_line_of_its_own() -> None:
    found = run_page.progress(working(RunStatus.WAITING_FOR_BUDGET, collect.WAITING))
    assert found is not None
    assert (found.step, found.note) == (0, collect.WAITING)


@pytest.mark.parametrize(
    "status",
    [RunStatus.AWAITING_SIGN_IN, RunStatus.AWAITING_APPROVAL, RunStatus.READY, RunStatus.FAILED, RunStatus.CANCELED],
)
def test_a_run_that_is_not_working_has_no_steps(status: RunStatus) -> None:
    assert run_page.progress(working(status, "")) is None


def test_every_status_has_a_heading() -> None:
    assert set(run_page.LABELS) == set(RunStatus)


# --------------------------------------------------------------------------------- what the search has found
def _event_with_posts(db: Session, run: Run, labels: list[str | None]) -> Event:
    """An event the run searches, with a post per label (None: not read yet), each by a different person."""
    event = Event(key=f"pipeline-week-{uuid.uuid4().hex[:8]}", name="Pipeline Week 2026")
    db.add(event)
    db.flush()
    db.add(RunEvent(run_id=run.id, event_id=event.id, source="suggested", status=RunEventStatus.DONE))
    for label in labels:
        post_id = str(next(_ids))
        db.add(Post(id=post_id, url=f"https://www.linkedin.com/posts/{post_id}", author_url=f"{PEOPLE}{post_id}"))
        db.flush()
        db.add(EventPost(event_id=event.id, post_id=post_id, query='"Pipeline Week"'))
        if label is not None:
            db.add(PostLabel(post_id=post_id, event_id=event.id, label=label, confidence=Decimal("0.9"), method="rule"))
    db.commit()
    return event


@pytest.mark.db
def test_while_searching_the_share_of_events_searched_moves_the_bar(db: Session) -> None:
    run = make_run(db, status=RunStatus.COLLECTING)
    _event_with_posts(db, run, ["attending"])
    other = Event(key=f"growth-forum-{uuid.uuid4().hex[:8]}", name="Growth Forum")
    db.add(other)
    db.flush()
    db.add(RunEvent(run_id=run.id, event_id=other.id, source="suggested", status=RunEventStatus.SEARCHING))
    db.commit()
    found = run_page.found_so_far(db, run, ("verified",))
    assert (found.posts, found.people, found.emails) == (1, 1, 0)
    assert found.percent == round(0.5 * 100 / 3)  # one of two events searched, in the first of three steps


@pytest.mark.db
def test_while_reading_people_count_once_they_are_seen_at_the_events(db: Session) -> None:
    run = make_run(db, status=RunStatus.QUALIFYING)
    _event_with_posts(db, run, ["attending", "speaking", "not_attending", None])
    found = run_page.found_so_far(db, run, ("verified",))
    assert (found.posts, found.people, found.emails) == (4, 2, 0)  # not attending, or not read yet, doesn't count
    assert found.percent == round((1 + 3 / 4) * 100 / 3)  # three of four posts read, in the second step


@pytest.mark.db
def test_while_finding_emails_only_verified_ones_still_in_the_running_count(db: Session) -> None:
    run = make_run(db, status=RunStatus.ENRICHING)
    event = _event_with_posts(db, run, ["attending", "speaking", "attended"])
    posts = db.scalars(select(EventPost.post_id).where(EventPost.event_id == event.id).order_by(EventPost.post_id))
    now = datetime.now(UTC)
    people = [
        Person(linkedin_url=f"{PEOPLE}a", email_status="verified", enriched_at=now),
        Person(linkedin_url=f"{PEOPLE}b", email_status="unavailable", enriched_at=now),
        Person(linkedin_url=f"{PEOPLE}c"),  # not looked up yet
    ]
    db.add_all(people)
    db.flush()
    for person, post_id in zip(people, posts, strict=True):
        db.add(Lead(run_id=run.id, person_id=person.id, event_id=event.id, post_id=post_id, attendance="attending"))
    db.commit()
    found = run_page.found_so_far(db, run, ("verified",))
    assert found.emails == 1
    assert found.percent == round((2 + 2 / 3) * 100 / 3)  # two of three people looked up, in the last step


@pytest.mark.db
def test_the_search_page_shows_the_counters(db: Session) -> None:
    run = make_run(db, status=RunStatus.QUALIFYING)
    _event_with_posts(db, run, ["attending", "speaking"])
    client = TestClient(create_app())
    cookie = auth.open_session(db, run.account_id, now=datetime.now(UTC), lifetime=timedelta(days=1))
    db.commit()
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    page = client.get(f"/runs/{run.id}").text
    assert "Finding your people" in page
    assert 'id="found-people">2<' in page
    assert "1 event" in page
    status = client.get(f"/runs/{run.id}/status").json()
    assert status["found"] == {"posts": 2, "people": 2, "emails": 0, "percent": round((1 + 1) * 100 / 3)}
