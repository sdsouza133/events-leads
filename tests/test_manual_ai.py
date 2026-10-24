"""Manual AI mode (issue #9): with no Anthropic key, Claude's questions wait as tasks that an operator answers."""

import dataclasses
import html
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, queue
from app.config import Settings, get_settings
from app.main import create_app
from app.models import Account, AiTask, Job, JobStatus, Run, RunStatus, SpendEntry, SpendStatus
from app.pipeline.analyze import ANALYZE_SITE
from app.pipeline.base import JobContext
from app.pipeline.jobs import JOBS
from app.providers import manual
from app.providers.fakes import FakeLlm
from app.providers.manual import ManualLlm
from app.providers.registry import build_providers
from app.providers.types import AnswerPendingError, FetchedPage
from app.schemas import AttendanceLabel, PostToLabel
from app.worker import ANSWER_RECHECK, Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
TOKEN = "operator-token-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OPERATOR = "ops@b2bops.com"
EVENT: dict[str, Any] = {
    "name": "RevOps Summit 2026",
    "short_name": None,
    "hashtags": ["RevOps"],
    "start_date": "2026-11-12",
    "end_date": "2026-11-13",
    "location": "Austin, United States",
    "url": "https://revops.example/",
    "source_url": "https://news.example/revops-summit",
    "why": "RevOps leaders go to compare notes.",
}


def page(text: str = "Pipeline analytics for B2B SaaS.") -> FetchedPage:
    return FetchedPage(url="https://acmewidgets.com/", status=200, title="Acme Widgets", description=None, text=text)


ANALYSIS = FakeLlm().analyze_site([page()]).value


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(operator_api_token=TOKEN, admin_emails=OPERATOR)
    with TestClient(app) as test_client:
        yield test_client


def tasks(db: Session) -> list[AiTask]:
    db.expire_all()
    return list(db.scalars(select(AiTask).order_by(AiTask.id)))


def answer(db: Session, task_id: int, value: dict[str, Any]) -> None:
    task = db.get(AiTask, task_id)
    assert task is not None
    task.answer = value
    db.commit()


def open_task(db: Session, kind: str, question: dict[str, Any]) -> AiTask:
    task = AiTask(key=uuid.uuid4().hex, kind=kind, question=question)
    db.add(task)
    db.commit()
    return task


def text(response: httpx2.Response) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


# ---------------------------------------------------------------------------------------------- the stand-in
def test_a_question_waits_as_one_task_until_it_is_answered(db: Session) -> None:
    llm = ManualLlm()
    for _ in range(2):  # asked again while it waits: still one task
        with pytest.raises(AnswerPendingError):
            llm.analyze_site([page()])
    [task] = tasks(db)
    assert (task.kind, task.answer, task.run_id) == ("analyze_site", None, None)
    assert task.question["pages"][0] == {
        "url": "https://acmewidgets.com/",
        "title": "Acme Widgets",
        "description": None,
        "text": "Pipeline analytics for B2B SaaS.",
    }

    answer(db, task.id, ANALYSIS.model_dump(mode="json"))
    got = llm.analyze_site([page("The site reads a little differently today.")])  # the same site: the same task
    assert (got.value, got.cost_usd, got.model) == (ANALYSIS, Decimal(0), "manual")
    with pytest.raises(ValueError, match="no pages"):
        llm.analyze_site([])


def test_events_come_back_as_suggestions(db: Session) -> None:
    llm = ManualLlm()
    with pytest.raises(AnswerPendingError):
        llm.find_events(ANALYSIS, today=NOW.date())
    [task] = tasks(db)
    assert task.question == {"analysis": ANALYSIS.model_dump(mode="json"), "today": "2026-10-24"}
    answer(db, task.id, {"events": [EVENT]})
    [event] = llm.find_events(ANALYSIS, today=NOW.date() + timedelta(days=1)).value  # a day later: the same task
    assert (event.name, event.source_url) == ("RevOps Summit 2026", "https://news.example/revops-summit")


def test_each_run_asks_for_its_own_events_and_an_old_answer_keeps_only_events_in_the_window(db: Session) -> None:
    first, second = make_run(db), make_run(db)  # say, a company's free run and a run granted months later
    db.commit()
    llm = ManualLlm()
    for run in (first, second):
        with manual.asking_for(str(run.id)), pytest.raises(AnswerPendingError):
            llm.find_events(ANALYSIS, today=NOW.date())
    first_task, second_task = tasks(db)  # one question each, though the analysis is the same
    assert (first_task.run_id, second_task.run_id) == (first.id, second.id)

    answer(db, first_task.id, {"events": [EVENT]})
    with manual.asking_for(str(first.id)):
        assert len(llm.find_events(ANALYSIS, today=NOW.date()).value) == 1
        # Read again in February, the summit (November 12-13) is more than 60 days past: it's left out.
        assert llm.find_events(ANALYSIS, today=date(2027, 2, 1)).value == []


def test_questions_are_deleted_after_30_days_answered_or_not(db: Session) -> None:
    old_open = open_task(db, "find_events", {"analysis": {}, "today": "2026-09-01"})
    old_answered = open_task(db, "analyze_site", {"pages": []})
    recent = open_task(db, "label_posts", {"event": "RevOps Summit 2026", "posts": []})
    old_open.created_at = old_answered.created_at = NOW - timedelta(days=31)
    old_answered.answer = ANALYSIS.model_dump(mode="json")
    db.commit()
    settings = Settings()
    Worker(JobContext(settings=settings, providers=build_providers(settings), clock=lambda: NOW), {}).maintain()
    assert [task.id for task in tasks(db)] == [recent.id]


def test_every_post_gets_one_label_in_the_order_asked(db: Session) -> None:
    posts = [
        PostToLabel(post_id="p1", text="Heading to RevOps Summit!"),
        PostToLabel(post_id="p2", text="Back from it"),
    ]
    llm = ManualLlm()
    assert llm.label_posts("RevOps Summit 2026", []).value == []
    with pytest.raises(AnswerPendingError):
        llm.label_posts("RevOps Summit 2026", posts)
    [task] = tasks(db)
    answer(db, task.id, {"labels": [{"post_id": "p2", "label": "attended", "confidence": 0.9, "reason": "a recap"}]})
    labels = llm.label_posts("RevOps Summit 2026", list(reversed(posts))).value  # the same posts: the same task
    assert [(label.post_id, label.label) for label in labels] == [
        ("p2", AttendanceLabel.ATTENDED),
        ("p1", AttendanceLabel.UNCLEAR),  # the API wouldn't take an answer without it, but just in case
    ]


def test_a_post_edited_since_it_was_asked_about_is_asked_about_again(db: Session) -> None:
    llm = ManualLlm()
    asked = [PostToLabel(post_id="p1", author_headline="VP Sales", text="Speaking at RevOps Summit!")]
    with pytest.raises(AnswerPendingError):
        llm.label_posts("RevOps Summit 2026", asked)
    [task] = tasks(db)
    answer(db, task.id, {"labels": [{"post_id": "p1", "label": "speaking", "confidence": 0.9, "reason": "says so"}]})
    edited = [asked[0].model_copy(update={"text": "Sadly I can't make RevOps Summit after all."})]
    with pytest.raises(AnswerPendingError):  # the answer was about what the post said then
        llm.label_posts("RevOps Summit 2026", edited)
    assert [t.question["posts"][0]["text"] for t in tasks(db)] == [asked[0].text, edited[0].text]


def test_a_task_names_the_run_whose_job_asked(db: Session) -> None:
    run = make_run(db)
    db.commit()
    with manual.asking_for(str(run.id)), pytest.raises(AnswerPendingError):
        ManualLlm().analyze_site([page()])
    with manual.asking_for("not a run id"):
        assert manual.ASKING_RUN.get() is None
    assert manual.ASKING_RUN.get() is None
    [task] = tasks(db)
    assert task.run_id == run.id


# ------------------------------------------------------------------------------------------- a run, end to end
def test_a_run_waits_for_the_operator_then_carries_on_at_no_cost(client: TestClient, db: Session) -> None:
    run = make_run(db, domain="acmewidgets.com", status=RunStatus.ANALYZING, website_url="https://acmewidgets.com/")
    queue.enqueue(db, ANALYZE_SITE, {"run_id": str(run.id)}, run_after=NOW)
    db.commit()
    clock = Clock()
    settings = Settings()
    providers = dataclasses.replace(build_providers(settings), llm=ManualLlm())
    worker = Worker(JobContext(settings=settings, providers=providers, clock=clock), JOBS, name="worker-a")

    assert worker.run_once()
    db.expire_all()
    [job] = db.scalars(select(Job).where(Job.kind == ANALYZE_SITE)).all()
    assert (job.status, job.attempts, job.run_after) == (JobStatus.QUEUED, 0, NOW + ANSWER_RECHECK)
    [site_task] = tasks(db)
    assert site_task.run_id == run.id
    spent = db.scalars(select(SpendEntry.status).where(SpendEntry.run_id == run.id)).all()
    assert spent == [SpendStatus.RELEASED]  # held for the call, then given back: nobody billable was asked

    listed = client.get("/admin/api/tasks", headers=AUTH).json()["tasks"]
    assert [(t["id"], t["kind"], t["run"]["domain"], t["run"]["status"]) for t in listed] == [
        (site_task.id, "analyze_site", "acmewidgets.com", "analyzing")
    ]
    assert listed[0]["answer_schema"]["title"] == "SiteAnalysis"
    posted = client.post(f"/admin/api/tasks/{site_task.id}", json=ANALYSIS.model_dump(mode="json"), headers=AUTH)
    assert posted.json() == {"id": site_task.id, "status": "answered"}

    clock.now += ANSWER_RECHECK
    assert worker.run_once()  # analyze_site, answered
    assert worker.run_once()  # find_events: the next question
    events_task = tasks(db)[-1]
    assert events_task.kind == "find_events"
    client.post(f"/admin/api/tasks/{events_task.id}", json={"events": [EVENT]}, headers=AUTH)
    clock.now += ANSWER_RECHECK
    assert worker.run_once()

    db.expire_all()
    done = db.get(Run, run.id)
    assert done is not None
    assert done.status == RunStatus.AWAITING_APPROVAL
    assert [event["name"] for event in done.suggested_events or []] == ["RevOps Summit 2026"]
    assert done.spent_usd == 0
    assert client.get("/admin/api/tasks", headers=AUTH).json() == {"tasks": []}  # answered ones aren't listed


# ------------------------------------------------------------------------------------------------- the API
def test_the_api_needs_the_operator_token(client: TestClient, db: Session) -> None:
    assert client.get("/admin/api/tasks").status_code == 401
    wrong = client.get("/admin/api/tasks", headers={"Authorization": "Bearer wrong"})
    assert (wrong.status_code, wrong.headers["www-authenticate"]) == (401, "Bearer")
    assert client.get("/admin/api/tasks", headers={b"Authorization": "Bearer t\u00f6ken".encode()}).status_code == 401
    assert client.get("/admin/api/tasks", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.post("/admin/api/tasks/1", json={}).status_code == 401
    listed = client.get("/admin/api/tasks", headers=AUTH)
    assert (listed.status_code, listed.json(), listed.headers["cache-control"]) == (200, {"tasks": []}, "no-store")


def test_without_a_token_set_there_is_no_api(db: Session) -> None:
    settings = Settings()  # no OPERATOR_API_TOKEN
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        assert client.get("/admin/api/tasks", headers=AUTH).status_code == 404


def test_an_answer_must_fit_the_schema(client: TestClient, db: Session) -> None:
    task = open_task(db, "analyze_site", {"pages": []})
    response = client.post(f"/admin/api/tasks/{task.id}", json={"company_name": "Acme"}, headers=AUTH)
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "the answer doesn't fit the schema"
    assert "offering: Field required" in body["problems"]
    assert client.post(f"/admin/api/tasks/{task.id}", content=b"not json", headers=AUTH).status_code == 422
    assert client.post(f"/admin/api/tasks/{task.id}", json=["a list"], headers=AUTH).status_code == 422
    db.refresh(task)
    assert task.answer is None


def test_events_must_be_in_the_window_asked_about_and_listed_once(client: TestClient, db: Session) -> None:
    task = open_task(db, "find_events", {"analysis": {}, "today": "2026-10-24"})
    next_year = {**EVENT, "name": "Next Year Expo", "start_date": "2027-06-01", "end_date": "2027-06-02"}
    response = client.post(f"/admin/api/tasks/{task.id}", json={"events": [EVENT, EVENT, next_year]}, headers=AUTH)
    assert response.status_code == 422
    assert response.json() == {
        "error": "the answer doesn't fit the question",
        "problems": ["events.1: the same event twice", "events.2: not between 2026-08-25 and 2027-01-22"],
    }


def test_labels_cover_each_post_once_and_a_task_is_answered_once(client: TestClient, db: Session) -> None:
    posts = [{"post_id": f"p{i}", "author_headline": None, "text": "See you there"} for i in (1, 2)]
    task = open_task(db, "label_posts", {"event": "RevOps Summit 2026", "posts": posts})
    one = {"post_id": "p1", "label": "attending", "confidence": 0.9, "reason": "says they'll be there"}
    short = client.post(f"/admin/api/tasks/{task.id}", json={"labels": [one]}, headers=AUTH)
    assert short.json()["problems"] == ["labels: give exactly one label for each post asked about"]

    both = {"labels": [one, {**one, "post_id": "p2"}]}
    assert client.post(f"/admin/api/tasks/{task.id}", json=both, headers=AUTH).status_code == 200
    db.refresh(task)
    assert task.answer == both
    assert task.answered_at is not None
    assert client.post(f"/admin/api/tasks/{task.id}", json=both, headers=AUTH).status_code == 409
    assert client.post("/admin/api/tasks/999999", json=both, headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------------------------- the console
@pytest.mark.parametrize(
    ("token", "says"),
    [
        (TOKEN, "1 question is waiting for an answer at /admin/api/tasks"),
        (None, "1 question is waiting for an answer, but OPERATOR_API_TOKEN isn't set on this service"),
    ],
)
def test_the_console_says_how_many_questions_are_waiting_and_where(db: Session, token: str | None, says: str) -> None:
    settings = Settings(
        providers="real", operator_api_token=token, apify_token="b", apollo_api_key="c", admin_emails=OPERATOR
    )
    open_task(db, "find_events", {"analysis": {}, "today": "2026-10-24"})
    answered = open_task(db, "analyze_site", {"pages": []})
    answer(db, answered.id, ANALYSIS.model_dump(mode="json"))
    operator = Account(email=OPERATOR, domain="b2bops.com")
    db.add(operator)
    db.flush()
    cookie = auth.open_session(db, operator.id, now=datetime.now(UTC), lifetime=timedelta(days=1))
    db.commit()

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, cookie)
        page_text = text(client.get("/admin"))
    assert "Manual AI mode is on" in page_text
    assert says in page_text
