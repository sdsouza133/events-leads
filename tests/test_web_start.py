"""Starting a run from the web (issue #8): the form, the emailed one-time link, sessions, and who sees a run."""

import logging
import re
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import create_app
from app.models import Account, Job, JobStatus, LoginToken, OutboxEmail, Run, RunGrant, RunStatus, UserSession
from app.pipeline.analyze import ANALYZE_SITE
from app.pipeline.base import JobContext
from app.pipeline.jobs import JOBS
from app.pipeline.sign_in import SEND_SIGN_IN
from app.providers.registry import build_providers
from app.web import start
from app.worker import Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

EMAIL = "jo@acmewidgets.com"  # a public suffix: ".example" is reserved, so not a website
LINK = re.compile(r"/sign-in\?token=([A-Za-z0-9_-]+)")


@pytest.fixture
def settings() -> Settings:
    return Settings(admin_emails="ops@b2bops.com")


@pytest.fixture
def client(db: Session, settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


def csrf(client: TestClient) -> str:
    """The CSRF token a real browser would have: the cookie set by the first page it opened."""
    if auth.CSRF_COOKIE not in client.cookies:
        client.get("/")
    return client.cookies[auth.CSRF_COOKIE]


def ask(client: TestClient, website: str = "acmewidgets.com", email: str = EMAIL) -> str:
    response = client.post("/start", data={"website": website, "email": email, "csrf": csrf(client)})
    return response.text


def send_links(settings: Settings) -> None:
    """What the worker does with the sign-in jobs the form queued."""
    w = Worker(JobContext(settings=settings, providers=build_providers(settings)), {SEND_SIGN_IN: JOBS[SEND_SIGN_IN]})
    while w.run_once():
        pass


def token_sent_to(db: Session, address: str = EMAIL) -> str:
    db.expire_all()
    email = db.scalars(select(OutboxEmail).where(OutboxEmail.to == address).order_by(OutboxEmail.id.desc())).first()
    assert email is not None
    match = LINK.search(email.text)
    assert match is not None
    return match.group(1)


def sign_in(client: TestClient, token: str) -> str:
    """Open the link and press the button, as a person would. Returns where it took them."""
    assert client.get(f"/sign-in?token={token}").status_code == 200
    response = client.post("/sign-in", data={"token": token, "csrf": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def runs(db: Session) -> list[Run]:
    db.expire_all()
    return list(db.scalars(select(Run).order_by(Run.created_at)))


def jobs(db: Session, kind: str) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == kind)))


# ---------------------------------------------------------------------------------------------------- the form
def test_the_start_page_asks_for_a_website_and_a_work_email(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="website"' in response.text
    assert 'name="email"' in response.text
    assert auth.CSRF_COOKIE in response.cookies
    assert response.headers["Cache-Control"] == "no-store"


def test_a_form_posted_without_the_page_s_token_is_refused(client: TestClient, db: Session) -> None:
    response = client.post("/start", data={"website": "acmewidgets.com", "email": EMAIL, "csrf": "forged"})
    assert response.status_code == 400
    assert runs(db) == []


@pytest.mark.parametrize(
    ("website", "email", "message"),
    [
        ("not a website", EMAIL, "Enter your company&#39;s website"),
        ("acmewidgets.com", "jo", "Enter your work email address."),
        ("acmewidgets.com", "jo@sam@acmewidgets.com", "Enter your work email address."),
        ("acmewidgets.com", "jo smith@acmewidgets.com", "Enter your work email address."),
        ("acmewidgets.com", "jo\x1b@acmewidgets.com", "Enter your work email address."),
        ("gmail.com", "jo@gmail.com", "Use your work email."),
        ("acmewidgets.com", "jo@otherwidgets.com", "Use an email address at acmewidgets.com"),
    ],
)
def test_each_problem_is_explained_next_to_its_field(
    client: TestClient, db: Session, website: str, email: str, message: str
) -> None:
    response = client.post("/start", data={"website": website, "email": email, "csrf": csrf(client)})
    assert response.status_code == 400
    assert message in response.text
    assert 'aria-invalid="true"' in response.text
    assert runs(db) == []


def test_a_valid_form_creates_the_run_and_queues_its_sign_in_email(client: TestClient, db: Session) -> None:
    assert "Check your email" in ask(client, website="https://www.acmewidgets.com/about")
    [run] = runs(db)
    assert (run.status, run.domain, run.website_url) == (
        RunStatus.AWAITING_SIGN_IN,
        "acmewidgets.com",
        "https://www.acmewidgets.com/about",
    )
    account = db.get(Account, run.account_id)
    assert account is not None
    assert (account.email, account.domain, account.verified_at) == (EMAIL, "acmewidgets.com", None)
    [job] = jobs(db, SEND_SIGN_IN)
    assert job.payload == {"account_id": str(account.id), "run_id": str(run.id)}


def test_asking_again_resends_the_link_for_the_same_run(client: TestClient, db: Session) -> None:
    ask(client)
    ask(client, website="acmewidgets.com/pricing", email="JO@acmewidgets.com")
    [run] = runs(db)
    assert run.website_url == "https://acmewidgets.com/pricing"
    assert len(jobs(db, SEND_SIGN_IN)) == 1  # the first is still waiting to go out: no second copy


def test_too_many_attempts_are_refused(db: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(sign_ups_per_hour=2)
    client = TestClient(app)
    for _ in range(2):
        ask(client)
    response = client.post("/start", data={"website": "acmewidgets.com", "email": EMAIL, "csrf": csrf(client)})
    assert response.status_code == 429
    assert "Too many attempts" in response.text


def test_a_company_that_used_its_free_run_is_told_so(client: TestClient, db: Session) -> None:
    make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    db.commit()
    page = ask(client)
    assert "acmewidgets.com has already used its free list" in page
    assert 'href="/sign-in-again"' in page  # whoever asked for it can still get back in
    assert jobs(db, SEND_SIGN_IN) == []


def test_a_run_that_stopped_before_spending_anything_does_not_use_the_free_run(client: TestClient, db: Session) -> None:
    make_run(db, domain="acmewidgets.com", status=RunStatus.FAILED)  # the website couldn't be read
    db.commit()
    assert "Check your email" in ask(client)


def test_a_run_an_operator_granted_lets_the_company_start_another(client: TestClient, db: Session) -> None:
    make_run(db, domain="acmewidgets.com", status=RunStatus.READY)
    db.add(RunGrant(domain="acmewidgets.com", note="a second list"))
    db.commit()
    assert "Check your email" in ask(client)


def test_while_new_runs_are_paused_the_form_takes_no_requests(db: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(pause_new_runs=True)
    with TestClient(app) as client:
        page = client.get("/")
        assert "We're not taking new requests right now" in page.text
        assert 'action="/start"' not in page.text
        response = client.post("/start", data={"website": "acmewidgets.com", "email": EMAIL, "csrf": csrf(client)})
    assert response.status_code == 503
    assert runs(db) == []
    assert jobs(db, SEND_SIGN_IN) == []


# ----------------------------------------------------------------------------------------------- the link
def test_the_worker_emails_a_link_and_keeps_only_its_hash(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    db.expire_all()
    [link] = db.scalars(select(LoginToken)).all()
    assert link.token_hash == auth.digest(token)
    assert token not in str(link.__dict__)
    assert link.expires_at - link.created_at <= timedelta(minutes=30, seconds=5)
    assert all(token not in str(job.payload) for job in db.scalars(select(Job)))


def test_opening_the_link_does_not_use_it(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    for _ in range(2):  # a mail scanner opening it first doesn't spend it
        response = client.get(f"/sign-in?token={token}")
        assert response.status_code == 200
        assert "Sign in and continue" in response.text
    db.expire_all()
    assert db.scalars(select(LoginToken.used_at)).one() is None


def test_signing_in_starts_reading_the_website(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    [run] = runs(db)
    assert sign_in(client, token_sent_to(db)) == f"/runs/{run.id}"

    [started] = runs(db)
    assert (started.status, started.status_detail) == (RunStatus.ANALYZING, start.READING_SITE)
    [job] = jobs(db, ANALYZE_SITE)
    assert (job.payload, job.status) == ({"run_id": str(run.id)}, JobStatus.QUEUED)
    account = db.get(Account, run.account_id)
    assert account is not None
    assert account.verified_at is not None
    session_cookie = client.cookies[auth.SESSION_COOKIE]
    assert db.scalars(select(UserSession.token_hash)).one() == auth.digest(session_cookie)


def test_the_session_cookie_is_http_only_and_lax(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    client.get(f"/sign-in?token={token}")
    response = client.post("/sign-in", data={"token": token, "csrf": csrf(client)}, follow_redirects=False)
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(auth.SESSION_COOKIE))
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie  # plain http on a developer's machine


def test_cookies_are_secure_wherever_the_site_is_served_over_https(db: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(base_url="https://leads.example")
    response = TestClient(app).get("/")
    assert "Secure" in response.headers["set-cookie"]


def test_a_link_works_once(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    sign_in(client, token)
    again = client.post("/sign-in", data={"token": token, "csrf": csrf(client)}, follow_redirects=False)
    assert again.status_code == 400
    assert "This link has expired" in again.text


def test_a_link_pressed_twice_at_once_signs_in_once(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    form = {"token": token, "csrf": csrf(client)}
    first = get_sessionmaker()()  # the first press: it has used the link and not committed yet
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert auth.redeem_link(first, token, now=datetime.now(UTC)) is not None
        second = pool.submit(client.post, "/sign-in", data=form, follow_redirects=False)
        with pytest.raises(TimeoutError):
            second.result(timeout=1)  # the second press waits for the link's row lock
        first.commit()
        assert second.result(timeout=10).status_code == 400
    finally:
        first.close()  # if an assertion failed before the commit, this lets the waiting press finish
        pool.shutdown()


def test_an_expired_link_says_so_before_anyone_presses_the_button(
    client: TestClient, db: Session, settings: Settings
) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    link = db.scalars(select(LoginToken)).one()
    link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    response = client.get(f"/sign-in?token={token}")
    assert response.status_code == 400
    assert "This link has expired" in response.text
    assert 'href="/sign-in-again"' in response.text


def test_a_sign_in_without_the_page_s_token_is_refused(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    response = client.post("/sign-in", data={"token": token, "csrf": "forged"}, follow_redirects=False)
    assert response.status_code == 400
    db.expire_all()
    assert db.scalars(select(LoginToken.used_at)).one() is None  # a forged post doesn't spend the link


def test_only_one_of_two_pending_runs_gets_the_company_s_free_run(
    client: TestClient, db: Session, settings: Settings
) -> None:
    ask(client, email="jo@acmewidgets.com")
    ask(client, email="sam@acmewidgets.com")  # a colleague asked too, before anyone signed in
    send_links(settings)
    sign_in(client, token_sent_to(db, "jo@acmewidgets.com"))
    sign_in(client, token_sent_to(db, "sam@acmewidgets.com"))

    first, second = runs(db)
    assert first.status == RunStatus.ANALYZING
    assert (second.status, second.status_detail) == (RunStatus.CANCELED, start.FREE_RUN_USED)
    assert len(jobs(db, ANALYZE_SITE)) == 1


def test_two_sign_ins_at_once_cannot_both_take_the_company_s_free_run(
    client: TestClient, db: Session, settings: Settings
) -> None:
    ask(client, email="jo@acmewidgets.com")
    ask(client, email="sam@acmewidgets.com")
    send_links(settings)
    jo, sam = token_sent_to(db, "jo@acmewidgets.com"), token_sent_to(db, "sam@acmewidgets.com")
    form = {"token": sam, "csrf": csrf(client)}
    first = get_sessionmaker()()  # Jo's sign-in: it holds the company's lock and has started Jo's run, uncommitted
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        now = datetime.now(UTC)
        link = auth.redeem_link(first, jo, now=now)
        assert link is not None
        assert link.run_id is not None
        start._begin(first, settings, link.run_id, now=now)
        second = pool.submit(client.post, "/sign-in", data=form, follow_redirects=False)
        with pytest.raises(TimeoutError):
            second.result(timeout=1)  # Sam's sign-in waits for the company's lock
        first.commit()
        assert second.result(timeout=10).status_code == 303
    finally:
        first.close()  # if an assertion failed before the commit, this lets the waiting sign-in finish
        pool.shutdown()

    jo_run, sam_run = runs(db)
    assert jo_run.status == RunStatus.ANALYZING
    assert (sam_run.status, sam_run.status_detail) == (RunStatus.CANCELED, start.FREE_RUN_USED)
    assert len(jobs(db, ANALYZE_SITE)) == 1


# ------------------------------------------------------------------------------------------- signing in again
def test_someone_signed_out_gets_a_new_link_to_the_same_run(
    client: TestClient, db: Session, settings: Settings
) -> None:
    run = signed_in_run(client, db, settings)
    client.post("/sign-out", data={"csrf": csrf(client)})
    assert 'href="/sign-in-again"' in client.get(f"/runs/{run.id}").text  # the page says how to get back in
    assert client.get("/sign-in-again").status_code == 200

    response = client.post("/sign-in-again", data={"email": EMAIL, "csrf": csrf(client)})
    assert response.status_code == 200
    assert "Check your email" in response.text
    send_links(settings)
    assert sign_in(client, token_sent_to(db)) == f"/runs/{run.id}"
    assert [r.id for r in runs(db)] == [run.id]  # no new run
    assert len(jobs(db, ANALYZE_SITE)) == 1  # and the run wasn't started again


def test_asking_for_a_new_link_does_not_say_who_has_an_account(client: TestClient, db: Session) -> None:
    response = client.post("/sign-in-again", data={"email": "nobody@acmewidgets.com", "csrf": csrf(client)})
    assert response.status_code == 200
    assert "If <strong>nobody@acmewidgets.com</strong> has asked us for a list" in response.text
    assert jobs(db, SEND_SIGN_IN) == []


def test_someone_whose_runs_were_deleted_signs_in_to_the_start_page(
    client: TestClient, db: Session, settings: Settings
) -> None:
    db.add(Account(email=EMAIL, domain="acmewidgets.com"))
    db.commit()
    client.post("/sign-in-again", data={"email": EMAIL, "csrf": csrf(client)})
    send_links(settings)
    assert sign_in(client, token_sent_to(db)) == "/"


def test_an_operator_s_link_opens_the_console(client: TestClient, db: Session, settings: Settings) -> None:
    client.post("/sign-in-again", data={"email": "Ops@B2BOps.com", "csrf": csrf(client)})
    db.expire_all()
    account = db.scalar(select(Account).where(Account.email == "ops@b2bops.com"))
    assert account is not None  # made on their first sign-in: operators don't ask for a run first
    send_links(settings)
    assert sign_in(client, token_sent_to(db, "ops@b2bops.com")) == "/admin"
    assert client.get("/admin").status_code == 200


def test_an_operator_with_a_run_of_their_own_still_lands_on_the_console(
    client: TestClient, db: Session, settings: Settings
) -> None:
    signed_in_run(client, db, settings, email="ops@b2bops.com")
    client.post("/sign-out", data={"csrf": csrf(client)})
    client.post("/sign-in-again", data={"email": "ops@b2bops.com", "csrf": csrf(client)})
    send_links(settings)
    assert sign_in(client, token_sent_to(db, "ops@b2bops.com")) == "/admin"


def test_a_new_link_needs_the_page_s_token_and_an_address(client: TestClient, db: Session) -> None:
    ask(client)
    forged = client.post("/sign-in-again", data={"email": EMAIL, "csrf": "forged"})
    assert forged.status_code == 400
    unreadable = client.post("/sign-in-again", data={"email": "jo", "csrf": csrf(client)})
    assert unreadable.status_code == 400
    assert 'aria-invalid="true"' in unreadable.text
    assert len(jobs(db, SEND_SIGN_IN)) == 1  # only the form's


def test_new_links_count_toward_the_hourly_limit(db: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(sign_ups_per_hour=2)
    client = TestClient(app)
    ask(client)
    client.post("/sign-in-again", data={"email": EMAIL, "csrf": csrf(client)})
    response = client.post("/sign-in-again", data={"email": EMAIL, "csrf": csrf(client)})
    assert response.status_code == 429
    assert "Too many attempts" in response.text


# --------------------------------------------------------------------------------------------- the run page
def signed_in_run(client: TestClient, db: Session, settings: Settings, email: str = EMAIL) -> Run:
    ask(client, website=email.rsplit("@", 1)[1], email=email)
    send_links(settings)
    sign_in(client, token_sent_to(db, email))
    return next(run for run in runs(db) if db.get(Account, run.account_id).email == email)  # type: ignore[union-attr]


def test_the_requester_sees_their_run_and_its_progress(client: TestClient, db: Session, settings: Settings) -> None:
    run = signed_in_run(client, db, settings)
    response = client.get(f"/runs/{run.id}")
    assert response.status_code == 200
    assert "Reading your website" in response.text
    assert "/static/run.js" in response.text  # it checks back while the run works
    status = client.get(f"/runs/{run.id}/status")
    assert status.json() == {"status": "analyzing", "label": "Reading your website", "detail": start.READING_SITE}


def test_nobody_else_can_see_a_run(client: TestClient, db: Session, settings: Settings) -> None:
    theirs = make_run(db, domain="other.example", status=RunStatus.READY)
    db.commit()
    signed_in_run(client, db, settings)
    assert client.get(f"/runs/{theirs.id}").status_code == 404  # not "forbidden": ids can't be probed
    assert client.get(f"/runs/{theirs.id}/status").status_code == 404
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404


def test_an_operator_sees_every_run(client: TestClient, db: Session, settings: Settings) -> None:
    theirs = make_run(db, domain="other.example", status=RunStatus.READY)
    db.commit()
    signed_in_run(client, db, settings, email="ops@b2bops.com")
    assert client.get(f"/runs/{theirs.id}").status_code == 200


def test_a_run_page_asks_a_stranger_to_sign_in(client: TestClient, db: Session) -> None:
    run = make_run(db)
    db.commit()
    response = client.get(f"/runs/{run.id}")
    assert response.status_code == 401
    assert "Sign in to see this page" in response.text


def test_signing_out_ends_the_session(client: TestClient, db: Session, settings: Settings) -> None:
    run = signed_in_run(client, db, settings)
    cookie = client.cookies[auth.SESSION_COOKIE]
    response = client.post("/sign-out", data={"csrf": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.scalars(select(UserSession.revoked_at)).one() is not None
    client.cookies.set(auth.SESSION_COOKIE, cookie)  # even a copy of the old cookie no longer works
    assert client.get(f"/runs/{run.id}").status_code == 401


# --------------------------------------------------------------------------------------------- the outbox
def test_a_developer_reads_the_outbox_to_sign_in_locally(db: Session) -> None:
    dev = Settings(env="dev")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: dev
    client = TestClient(app)
    ask(client)
    send_links(dev)
    page = client.get("/dev/outbox")
    assert page.status_code == 200
    assert "Your sign-in link for Event Leads" in page.text
    assert '<a href="http://localhost:8000/sign-in?token=' in page.text  # a link you can click


@pytest.mark.parametrize("env", ["staging", "production"])
def test_elsewhere_the_outbox_is_closed_to_the_public(db: Session, env: str) -> None:
    app = create_app()
    deployed = Settings(
        env=env,
        providers="real",
        secret_key="x" * 40,
        database_url="postgresql://el:el@db.example/el",
        qa_token="qa-secret",
    )
    app.dependency_overrides[get_settings] = lambda: deployed
    client = TestClient(app)
    assert client.get("/dev/outbox").status_code == 404
    assert client.get("/dev/outbox/latest", params={"to": EMAIL}).status_code == 404


def test_the_qa_suite_reads_a_preview_s_newest_email_with_its_token(db: Session) -> None:
    preview = Settings(qa_token="qa-secret")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: preview
    client = TestClient(app)
    ask(client)
    send_links(preview)
    headers = {"X-QA-Token": "qa-secret"}
    latest = client.get("/dev/outbox/latest", params={"to": EMAIL}, headers=headers)
    assert latest.status_code == 200
    assert LINK.search(latest.json()["text"]) is not None
    assert client.get("/dev/outbox/latest", params={"to": EMAIL}, headers={"X-QA-Token": "wrong"}).status_code == 404


# -------------------------------------------------------------------------------------------- what's logged
def test_no_link_session_or_address_reaches_the_logs(
    client: TestClient, db: Session, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ask(client)
    send_links(settings)
    token = token_sent_to(db)
    sign_in(client, token)
    cookie = client.cookies[auth.SESSION_COOKIE]
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert token not in logged
    assert cookie not in logged
    assert "@acmewidgets.com" not in logged  # nor the requester's address, even masked: lines name the account
    [requested] = [record for record in caplog.records if record.getMessage() == "sign-in link requested"]
    account = db.scalar(select(Account))
    assert account is not None
    assert requested.__dict__["account_id"] == str(account.id)


def test_a_new_run_gets_the_run_budget(client: TestClient, db: Session, settings: Settings) -> None:
    ask(client)
    [run] = runs(db)
    assert run.budget_usd == settings.run_budget_usd == Decimal("3.00")
