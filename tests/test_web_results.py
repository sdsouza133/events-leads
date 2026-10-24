"""A finished run's list (issue #8, FR-8): the table on its page, the CSV download, and expiry."""

import csv
import html
import io
import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.config import Settings, get_settings
from app.main import create_app
from app.models import Account, Event, Lead, Person, Post, Run, RunStatus
from app.pipeline import enrich
from app.web import results
from tests.factories import make_run

pytestmark = pytest.mark.db

OPERATOR = "ops@b2bops.com"
NOW = datetime.now(UTC)
_ids = itertools.count(7_200_000_000_000_000_000)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(admin_emails=OPERATOR)
    with TestClient(app) as test_client:
        yield test_client


def finished_run(db: Session, *, expires_in: timedelta = timedelta(days=30)) -> Run:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    run.status_detail = enrich.READY
    run.completed_at = NOW
    run.results_expire_at = NOW + expires_in
    db.commit()
    return run


def lead(
    db: Session,
    run: Run,
    name: str,
    *,
    event: str = "Pipeline Week 2026",
    attendance: str = "attending",
    score: int = 80,
    included: bool = True,
    post_url: str | None = None,
) -> None:
    handle = name.lower().replace(" ", "-").strip("=")
    key = event.lower().replace(" ", "-")
    found = db.scalar(select(Event).where(Event.key == key))
    if found is None:
        found = Event(key=key, name=event)
        db.add(found)
    person = Person(
        linkedin_url=f"https://www.linkedin.com/in/{handle}",
        name=name,
        title="VP Marketing",
        company_name="Northwind Labs",
        email=f"{handle}@northwind.example",
        email_status="verified",
    )
    post_id = str(next(_ids))
    post = Post(id=post_id, url=post_url or f"https://www.linkedin.com/posts/{handle}-{post_id}", text="See you there")
    db.add_all([person, post])
    db.flush()
    db.add(
        Lead(
            run_id=run.id,
            person_id=person.id,
            event_id=found.id,
            post_id=post_id,
            attendance=attendance,
            icp_score=score,
            icp_reasons=["title matches 'marketing'"],
            included=included,
            exclude_reason=None if included else "ICP score 20 is below 40",
        )
    )
    db.commit()


def sign_in_as(client: TestClient, db: Session, account_id: uuid.UUID) -> None:
    cookie = auth.open_session(db, account_id, now=datetime.now(UTC), lifetime=timedelta(days=1))
    db.commit()
    client.cookies.set(auth.SESSION_COOKIE, cookie)


def text(response: httpx2.Response) -> str:
    """The page's text as a person reads it: entities turned back into characters, line breaks into spaces."""
    return " ".join(html.unescape(response.text).split())


def csv_rows(response: httpx2.Response) -> list[list[str]]:
    assert response.content.startswith(b"\xef\xbb\xbf")  # UTF-8 with a byte-order mark, so Excel reads it right
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


# ------------------------------------------------------------------------------------------------ the page
def test_the_list_shows_who_was_included_best_fit_first(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, "Ana Attendee", score=75, attendance="attending")
    lead(db, run, "Sam Speaker", score=75, attendance="speaking", event="RevOps Summit 2026")
    lead(db, run, "Top Fit", score=95, attendance="attended")
    lead(db, run, "Left Out", score=20, included=False)
    sign_in_as(client, db, run.account_id)

    page = text(client.get(f"/runs/{run.id}"))
    assert "3 leads, ready to go" in page
    assert "From Pipeline Week 2026 and RevOps Summit 2026 · best fit first" in page
    names = [name for name in ["Top Fit", "Sam Speaker", "Ana Attendee"] if name in page]
    assert names == ["Top Fit", "Sam Speaker", "Ana Attendee"]
    assert page.index("Top Fit") < page.index("Sam Speaker") < page.index("Ana Attendee")  # a speaker before a guest
    assert "Left Out" not in page
    assert 'href="https://www.linkedin.com/in/top-fit"' in page
    assert '<span class="badge tone-warm">Speaking</span> RevOps Summit 2026' in page  # the badge, then the event
    assert f'href="/runs/{run.id}/leads.csv"' in page


def test_the_list_can_show_one_kind_of_attendee(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, "Ana Attendee", attendance="attending")
    lead(db, run, "Sam Speaker", attendance="speaking")
    lead(db, run, "Sue Speaker", attendance="speaking", score=70)
    sign_in_as(client, db, run.account_id)

    everyone = text(client.get(f"/runs/{run.id}"))
    assert 'aria-current="true">All <span class="filter-count">3</span>' in everyone
    assert f'href="/runs/{run.id}?only=speaking#leads">Speaking <span class="filter-count">2</span>' in everyone
    assert "Exhibiting" not in everyone  # nobody exhibiting: no filter for it

    speakers = text(client.get(f"/runs/{run.id}?only=speaking"))
    assert "Sam Speaker" in speakers
    assert "Sue Speaker" in speakers
    assert "Ana Attendee" not in speakers
    assert "3 leads, ready to go" in speakers  # the heading still counts the whole list
    assert 'aria-current="true">Speaking' in speakers
    assert "Ana Attendee" in text(client.get(f"/runs/{run.id}?only=nonsense"))  # an unknown filter shows all


@pytest.mark.parametrize(
    ("name", "initials"), [("Ana Maria Attendee", "AA"), ("Prince", "P"), ("", ""), ("jo smith", "JS")]
)
def test_a_lead_s_initials_are_their_first_and_last(name: str, initials: str) -> None:
    assert results.Row(name, "", "", "", "", "", "Going", "", 50, []).initials == initials


def test_why_a_lead_fits_is_said_in_words() -> None:
    reasons = ["headline matches 'growth'", "seniority is vp", "industry matches 'Software'", "company size is 51-200"]
    row = results.Row("Jo", "", "", "", "", "", "Going", "", 100, reasons)
    assert row.matches == "Matches on title, seniority, industry and company size"
    assert results.Row("Jo", "", "", "", "", "", "Went", "", 0, []).matches == ""


def test_a_run_that_found_nobody_says_so(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    run.status_detail = enrich.NOTHING_FOUND
    db.commit()
    sign_in_as(client, db, run.account_id)
    page = text(client.get(f"/runs/{run.id}"))
    assert enrich.NOTHING_FOUND in page
    assert "leads.csv" not in page


def test_a_link_that_is_not_a_web_page_is_not_shown(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, "Odd Post", post_url="javascript:alert(1)")
    sign_in_as(client, db, run.account_id)
    page = client.get(f"/runs/{run.id}").text
    assert "javascript:" not in page
    assert "Odd Post" in page


# ------------------------------------------------------------------------------------------------- the CSV
def test_the_csv_has_every_column_in_the_same_order_as_the_page(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, "Ana Attendee", score=70)
    lead(db, run, "Top Fit", score=95, attendance="speaking")
    sign_in_as(client, db, run.account_id)

    response = client.get(f"/runs/{run.id}/leads.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="leads-acmewidgets.com-{NOW:%Y-%m-%d}.csv"'
    assert response.headers["cache-control"] == "no-store"
    header, first, second = csv_rows(response)
    assert header == results.COLUMNS
    assert first == [
        "Top Fit",
        "VP Marketing",
        "Northwind Labs",
        "top-fit@northwind.example",
        "https://www.linkedin.com/in/top-fit",
        "Pipeline Week 2026",
        "Speaking",
        first[7],
        "95",
        "title matches 'marketing'",
    ]
    assert first[7].startswith("https://www.linkedin.com/posts/top-fit-")
    assert second[0] == "Ana Attendee"


def test_cells_that_a_spreadsheet_would_run_as_formulas_are_kept_as_text(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, '=HYPERLINK("https://evil.example","Click")')
    sign_in_as(client, db, run.account_id)
    _, row = csv_rows(client.get(f"/runs/{run.id}/leads.csv"))
    assert row[0] == '\'=HYPERLINK("https://evil.example","Click")'


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "\nx"])
def test_every_formula_start_is_neutralized(value: str) -> None:
    assert results._cell(value) == "'" + value
    assert results._cell("plain " + value) == "plain " + value


# ------------------------------------------------------------------------------------------- who and when
def test_expired_results_are_neither_shown_nor_downloadable(client: TestClient, db: Session) -> None:
    run = finished_run(db, expires_in=-timedelta(seconds=1))
    lead(db, run, "Top Fit")
    sign_in_as(client, db, run.account_id)
    page = text(client.get(f"/runs/{run.id}"))
    assert "This list has expired" in page
    assert "Top Fit" not in page
    download = client.get(f"/runs/{run.id}/leads.csv")
    assert download.status_code == 410
    assert "Top Fit" not in download.text


def test_only_the_requester_or_an_operator_can_download(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    lead(db, run, "Top Fit")
    stranger = Account(email="sam@otherwidgets.com", domain="otherwidgets.com")
    operator = Account(email=OPERATOR, domain="b2bops.com")
    db.add_all([stranger, operator])
    db.commit()

    assert client.get(f"/runs/{run.id}/leads.csv").status_code == 401
    sign_in_as(client, db, stranger.id)
    assert client.get(f"/runs/{run.id}/leads.csv").status_code == 404
    sign_in_as(client, db, operator.id)
    assert client.get(f"/runs/{run.id}/leads.csv").status_code == 200


def test_a_run_still_working_has_no_csv_yet(client: TestClient, db: Session) -> None:
    run = finished_run(db)
    run.status = RunStatus.ENRICHING
    db.commit()
    sign_in_as(client, db, run.account_id)
    assert client.get(f"/runs/{run.id}/leads.csv").status_code == 404
