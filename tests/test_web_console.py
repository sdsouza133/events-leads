"""The operator console (issue #9, FR-9): runs and their cost, the day's spend, the queue, canceling and granting."""

import html
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, budget, flags, queue
from app.config import Settings, get_settings
from app.main import create_app
from app.models import Account, BudgetDay, Flag, Job, JobStatus, Run, RunGrant, RunStatus
from app.pipeline import runs
from app.pipeline.analyze import ANALYZE_SITE
from app.web import console, start
from tests.factories import make_account, make_run

pytestmark = pytest.mark.db

OPERATOR = "ops@b2bops.com"


@pytest.fixture
def settings() -> Settings:
    return Settings(admin_emails=OPERATOR)


@pytest.fixture
def client(db: Session, settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


def sign_in_as(client: TestClient, db: Session, account_id: uuid.UUID) -> None:
    cookie = auth.open_session(db, account_id, now=datetime.now(UTC), lifetime=timedelta(days=1))
    db.commit()
    client.cookies.set(auth.SESSION_COOKIE, cookie)


def operator(client: TestClient, db: Session) -> Account:
    account = Account(email=OPERATOR, domain="b2bops.com")
    db.add(account)
    db.flush()
    sign_in_as(client, db, account.id)
    return account


def csrf(client: TestClient) -> str:
    if auth.CSRF_COOKIE not in client.cookies:
        client.get("/")
    return client.cookies[auth.CSRF_COOKIE]


def text(response: httpx2.Response) -> str:
    """The page's text as a person reads it: tags dropped, entities turned back into characters, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found = db.get(Run, run.id)
    assert found is not None
    return found


def grants(db: Session) -> list[RunGrant]:
    db.expire_all()
    return list(db.scalars(select(RunGrant).order_by(RunGrant.id)))


# ------------------------------------------------------------------------------------------------------ access
def test_only_operators_see_the_console(client: TestClient, db: Session) -> None:
    signed_out = client.get("/admin")
    assert signed_out.status_code == 401
    assert "Sign in to see this page" in signed_out.text

    requester = make_account(db, domain="acmewidgets.com")
    sign_in_as(client, db, requester.id)
    assert client.get("/admin").status_code == 404  # not "forbidden": it doesn't exist for them
    assert 'href="/admin"' not in client.get("/").text

    operator(client, db)
    page = client.get("/admin")
    assert page.status_code == 200
    assert ">Console</h1>" in page.text
    assert 'href="/admin">Console</a>' in client.get("/").text  # and every page links to it


def test_requesters_cannot_cancel_or_grant(client: TestClient, db: Session) -> None:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.COLLECTING)
    sign_in_as(client, db, run.account_id)
    token = csrf(client)
    assert client.get(f"/admin/runs/{run.id}/cancel").status_code == 404
    assert client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": token}).status_code == 404
    assert client.post("/admin/grants", data={"company": "acmewidgets.com", "csrf": token}).status_code == 404
    assert fresh(db, run).status == RunStatus.COLLECTING
    assert grants(db) == []


# ------------------------------------------------------------------------------------------------- what it shows
def test_runs_are_listed_newest_first_with_their_status_and_cost(client: TestClient, db: Session) -> None:
    older = make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    older.created_at = datetime.now(UTC) - timedelta(days=2)
    older.spent_usd, older.leads_count, older.apollo_credits_used = Decimal("1.234"), 42, 57
    newer = make_run(db, domain="northwind.com", status=RunStatus.COLLECTING)
    newer.spent_usd, newer.reserved_usd = Decimal("0.40"), Decimal("1.20")
    newer.status_detail, newer.error = "Finding posts about your events", "1 event failed: Apify 502"
    db.commit()
    operator(client, db)

    page = text(client.get("/admin"))
    assert page.index("northwind.com") < page.index("acmewidgets.com")
    assert "Searching Finding posts about your events Error: 1 event failed: Apify 502" in page
    assert "$0.40 of $3.00, $1.20 held" in page
    assert "Ready 42 $1.23 of $3.00 57 Apollo credits" in page
    requester = db.get(Account, older.account_id)
    assert requester is not None
    assert requester.email in page
    raw = client.get("/admin").text
    assert f'href="/runs/{older.id}"' in raw
    assert f'href="/admin/runs/{newer.id}/cancel"' in raw  # a run still going can be canceled
    assert f'href="/admin/runs/{older.id}/cancel"' not in raw  # a finished one can't


def test_todays_spend_is_shown_against_the_cap(client: TestClient, db: Session) -> None:
    db.add(BudgetDay(day=budget.utc_day(), spent_usd=Decimal("12.5"), reserved_usd=Decimal("0.75")))
    db.add(BudgetDay(day=budget.utc_day() - timedelta(days=1), spent_usd=Decimal("19.00")))  # yesterday's
    db.commit()
    operator(client, db)
    page = text(client.get("/admin"))
    assert "Spent $12.50 of the $20.00 daily cap, with $0.75 held for calls under way. $6.75 left." in page


def test_the_queue_shows_what_is_due_waiting_running_and_failed(client: TestClient, db: Session) -> None:
    run = make_run(db, status=RunStatus.FAILED)
    now = datetime.now(UTC)
    queue.enqueue(db, "analyze_site", {}, run_after=now - timedelta(minutes=1))
    queue.enqueue(db, "qualify", {}, run_after=now + timedelta(hours=8))  # the day's budget is used up
    db.add(Job(kind="collect_event", status=JobStatus.RUNNING, locked_by="worker-a", locked_at=now))
    db.add(
        Job(
            kind="enrich",
            payload={"run_id": str(run.id)},
            status=JobStatus.FAILED,
            attempts=5,
            last_error="ProviderError: Apollo rejected our credentials (401)",
        )
    )
    db.add(Job(kind="send_sign_in", payload={"run_id": None}, status=JobStatus.FAILED, attempts=5))
    db.commit()
    operator(client, db)

    response = client.get("/admin")
    page = text(response)
    assert "1 job due now, 1 waiting for later, 1 running, 2 failed." in page
    assert "enrich" in page
    assert "ProviderError: Apollo rejected our credentials (401)" in page
    assert f'href="/runs/{run.id}"' in response.text


def test_the_console_says_when_new_paid_work_is_paused(db: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(admin_emails=OPERATOR, pause_new_runs=True)
    with TestClient(app) as client:
        operator(client, db)
        page = client.get("/admin")
        assert "New paid work is paused" in text(page)
        assert "New runs Paused Set by PAUSE_NEW_RUNS on the server" in text(page)  # not "Open"
        assert 'role="switch" aria-checked="true"' in page.text  # the switch keeps its own state


# ---------------------------------------------------------------------------------------------------- canceling
def test_canceling_asks_first_then_stops_the_run(client: TestClient, db: Session) -> None:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.QUALIFYING)
    run.error = "budget ran out while labeling"
    db.commit()
    operator(client, db)

    confirm = client.get(f"/admin/runs/{run.id}/cancel")
    assert confirm.status_code == 200
    assert "Cancel the run for acmewidgets.com?" in text(confirm)
    assert fresh(db, run).status == RunStatus.QUALIFYING  # asking changes nothing

    response = client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": csrf(client)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/admin?notice=canceled#runs")
    canceled = fresh(db, run)
    assert (canceled.status, canceled.status_detail) == (RunStatus.CANCELED, console.CANCELED)
    assert canceled.completed_at is not None
    assert canceled.error == "budget ran out while labeling; canceled by an operator"
    assert "The run is canceled" in text(client.get("/admin?notice=canceled"))


def test_the_requester_sees_that_a_canceled_run_stopped(client: TestClient, db: Session) -> None:
    run = make_run(db, status=RunStatus.COLLECTING)
    db.commit()
    operator(client, db)
    client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": csrf(client)})
    sign_in_as(client, db, run.account_id)
    page = text(client.get(f"/runs/{run.id}"))
    assert "This run stopped" in page
    assert console.CANCELED in page


def test_a_finished_run_is_not_canceled(client: TestClient, db: Session) -> None:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    db.commit()
    operator(client, db)
    assert "The run for acmewidgets.com has finished" in text(client.get(f"/admin/runs/{run.id}/cancel"))
    response = client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": csrf(client)}, follow_redirects=False)
    assert response.headers["location"] == "/admin?notice=finished#runs"
    assert fresh(db, run).status == RunStatus.READY


def test_canceling_needs_the_page_s_token(client: TestClient, db: Session) -> None:
    run = make_run(db, status=RunStatus.COLLECTING)
    db.commit()
    operator(client, db)
    csrf(client)
    response = client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": "forged"})
    assert response.status_code == 400
    assert "This page had expired" in response.text
    assert fresh(db, run).status == RunStatus.COLLECTING


def test_canceling_a_run_that_does_not_exist_is_not_found(client: TestClient, db: Session) -> None:
    operator(client, db)
    assert client.get(f"/admin/runs/{uuid.uuid4()}/cancel").status_code == 404
    assert client.post(f"/admin/runs/{uuid.uuid4()}/cancel", data={"csrf": csrf(client)}).status_code == 404


def test_a_run_canceled_before_it_spent_anything_frees_the_company_s_run(client: TestClient, db: Session) -> None:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.ANALYZING)
    db.commit()
    assert not runs.has_run_left(db, "acmewidgets.com", free_runs=1)
    operator(client, db)
    client.post(f"/admin/runs/{run.id}/cancel", data={"csrf": csrf(client)})
    assert runs.has_run_left(db, "acmewidgets.com", free_runs=1)


# ----------------------------------------------------------------------------------------------------- granting
def test_a_grant_gives_a_company_one_more_run(client: TestClient, db: Session) -> None:
    make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    db.commit()
    assert not runs.has_run_left(db, "acmewidgets.com", free_runs=1)
    account = operator(client, db)

    response = client.post(
        "/admin/grants",
        data={"company": "https://www.AcmeWidgets.com/about", "note": " a second list for Q4 ", "csrf": csrf(client)},
        follow_redirects=False,
    )
    assert (response.status_code, response.headers["location"]) == (303, "/admin?notice=granted#grants")
    [grant] = grants(db)
    assert (grant.domain, grant.granted_by, grant.note) == ("acmewidgets.com", account.id, "a second list for Q4")
    assert runs.has_run_left(db, "acmewidgets.com", free_runs=1)

    page = text(client.get("/admin?notice=granted"))
    assert "Granted: the company can start another run." in page
    assert f"acmewidgets.com {OPERATOR} a second list for Q4" in page

    make_run(db, domain="acmewidgets.com", status=RunStatus.ANALYZING)  # the granted run, taken
    db.commit()
    assert not runs.has_run_left(db, "acmewidgets.com", free_runs=1)


@pytest.mark.parametrize(
    ("company", "note", "error"),
    [
        ("not a website", "", "Enter the company's website or domain"),
        ("", "", "Enter the company's website or domain"),
        ("acmewidgets.com", "x" * 301, "Keep the note to 300 characters."),
    ],
)
def test_a_grant_needs_a_company_and_a_short_note(
    client: TestClient, db: Session, company: str, note: str, error: str
) -> None:
    operator(client, db)
    response = client.post("/admin/grants", data={"company": company, "note": note, "csrf": csrf(client)})
    assert response.status_code == 400
    assert error in text(response)
    assert grants(db) == []


def test_a_grant_needs_the_page_s_token(client: TestClient, db: Session) -> None:
    operator(client, db)
    csrf(client)
    response = client.post("/admin/grants", data={"company": "acmewidgets.com", "csrf": "forged"})
    assert response.status_code == 400
    assert "This page had expired" in response.text
    assert grants(db) == []


# ------------------------------------------------------------------------------------------- running any company
def test_an_operator_runs_any_company_without_its_email(client: TestClient, db: Session) -> None:
    make_run(db, domain="acmewidgets.com", status=RunStatus.READY)  # the company has used its free run
    db.commit()
    account = operator(client, db)
    response = client.post(
        "/admin/runs",
        data={"website": "https://www.AcmeWidgets.com/about", "csrf": csrf(client)},
        follow_redirects=False,
    )
    run = db.scalars(select(Run).where(Run.account_id == account.id)).one()
    assert (response.status_code, response.headers["location"]) == (303, f"/runs/{run.id}")
    assert (run.domain, run.website_url, run.status, run.status_detail) == (
        "acmewidgets.com",
        "https://www.acmewidgets.com/about",
        RunStatus.ANALYZING,
        start.READING_SITE,
    )
    assert run.operator_run
    assert run.budget_usd == Decimal("3.00")  # the same cap as any run
    [job] = db.scalars(select(Job).where(Job.kind == ANALYZE_SITE)).all()
    assert job.payload == {"run_id": str(run.id)}
    assert "Getting your plan ready" in client.get(response.headers["location"]).text

    page = text(client.get("/admin"))
    assert "acmewidgets.com You Console Making the plan" in page


def test_an_operator_s_run_leaves_the_company_its_free_run(client: TestClient, db: Session) -> None:
    operator(client, db)
    client.post("/admin/runs", data={"website": "acmewidgets.com", "csrf": csrf(client)})
    assert runs.has_run_left(db, "acmewidgets.com", free_runs=1)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"website": "not a website"}, "Enter the company's website, for example acme.com."),
        ({"website": ""}, "Enter the company's website, for example acme.com."),
        ({"website": "acmewidgets.com", "csrf": "forged"}, "This page had expired"),
    ],
)
def test_running_a_company_needs_a_website_and_the_page_s_token(
    client: TestClient, db: Session, data: dict[str, str], message: str
) -> None:
    operator(client, db)
    response = client.post("/admin/runs", data={"csrf": csrf(client), **data})
    assert response.status_code == 400
    assert message in text(response)
    assert db.scalars(select(Run)).all() == []


def test_only_operators_run_companies(client: TestClient, db: Session) -> None:
    assert client.post("/admin/runs", data={"website": "acmewidgets.com"}).status_code == 401
    requester = make_account(db, domain="otherwidgets.com")
    sign_in_as(client, db, requester.id)
    assert client.post("/admin/runs", data={"website": "acmewidgets.com", "csrf": csrf(client)}).status_code == 404
    assert db.scalars(select(Run)).all() == []


def test_the_console_counts_the_runs_started_today(client: TestClient, db: Session) -> None:
    make_run(db).created_at = datetime.now(UTC) - timedelta(days=2)
    make_run(db, domain="northwind.com")
    db.commit()
    operator(client, db)
    assert "Runs today 1 Since midnight UTC" in text(client.get("/admin"))


# ------------------------------------------------------------------------------------------------- the switch
def test_the_switch_pauses_and_reopens_the_start_form(client: TestClient, db: Session) -> None:
    account = operator(client, db)
    assert 'role="switch" aria-checked="true"' in client.get("/admin").text

    paused = client.post("/admin/new-runs", data={"take": "no", "csrf": csrf(client)}, follow_redirects=False)
    assert (paused.status_code, paused.headers["location"]) == (303, "/admin?notice=paused")
    assert flags.is_on(db, flags.NEW_RUNS_PAUSED)
    assert 'role="switch" aria-checked="false"' in client.get("/admin").text
    assert "We're not taking new requests right now" in client.get("/").text
    form = {"website": "acmewidgets.com", "email": "jo@acmewidgets.com", "csrf": csrf(client)}
    assert client.post("/start", data=form).status_code == 503
    assert db.scalars(select(Run)).all() == []

    client.post("/admin/new-runs", data={"take": "yes", "csrf": csrf(client)})
    db.expire_all()
    assert not flags.is_on(db, flags.NEW_RUNS_PAUSED)
    assert 'action="/start"' in client.get("/").text
    assert db.get(Flag, flags.NEW_RUNS_PAUSED).changed_by == account.id  # type: ignore[union-attr]


def test_the_switch_needs_an_operator_and_the_page_s_token(client: TestClient, db: Session) -> None:
    assert client.post("/admin/new-runs", data={"take": "no"}).status_code == 401
    operator(client, db)
    assert client.post("/admin/new-runs", data={"take": "no", "csrf": "forged"}).status_code == 400
    assert client.post("/admin/new-runs", data={"take": "maybe", "csrf": csrf(client)}).status_code == 400
    assert not flags.is_on(db, flags.NEW_RUNS_PAUSED)
