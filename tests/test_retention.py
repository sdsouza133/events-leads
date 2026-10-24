"""Deleting personal data after 30 days (issue #18): what a round deletes, what it keeps, and that it is safe to
repeat and to run alongside the pipeline."""

import itertools
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_sessionmaker, session_scope
from app.models import (
    AiTask,
    Event,
    EventPost,
    Job,
    JobStatus,
    Lead,
    LoginToken,
    OutboxEmail,
    Person,
    Post,
    PostLabel,
    RateLimit,
    Run,
    RunStatus,
    SearchCache,
    UserSession,
)
from app.pipeline import retention
from app.pipeline.base import Defer, JobContext
from app.pipeline.jobs import JOBS
from app.providers.registry import build_providers
from app.worker import Worker
from tests.factories import make_account, make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=31)
RECENT = NOW - timedelta(days=29)
_ids = itertools.count(7_300_000_000_000_000_000)


def context() -> JobContext:
    settings = Settings()
    return JobContext(settings=settings, providers=build_providers(settings), clock=lambda: NOW)


def an_event(db: Session) -> Event:
    event = Event(key=f"pipeline-week-{uuid.uuid4().hex[:8]}", name="Pipeline Week 2026")
    db.add(event)
    db.flush()
    return event


def a_post(db: Session, *, fetched_at: datetime = RECENT) -> Post:
    post_id = str(next(_ids))
    post = Post(id=post_id, url=f"https://www.linkedin.com/posts/someone-{post_id}", text="See you there")
    post.fetched_at = fetched_at
    db.add(post)
    db.flush()
    return post


def a_person(db: Session, *, enriched_at: datetime | None = None, email_checked_at: datetime | None = None) -> Person:
    person = Person(
        linkedin_url=f"https://www.linkedin.com/in/someone-{uuid.uuid4().hex[:8]}",
        name="Sam Example",
        email="sam@northwind.example",
        enriched_at=enriched_at,
        email_checked_at=email_checked_at,
    )
    db.add(person)
    db.flush()
    return person


def a_run(
    db: Session, status: RunStatus, *, completed_at: datetime | None = None, expires: datetime | None = None
) -> Run:
    run = make_run(db, status=status)
    run.completed_at = completed_at
    run.results_expire_at = expires
    run.leads_count = 1
    db.flush()
    return run


def on_list(db: Session, run: Run, *, person: Person | None = None, post: Post | None = None) -> Lead:
    """A lead on the run's list. Its person and post are old unless given: only the list keeps them."""
    lead = Lead(
        run_id=run.id,
        person_id=(person or a_person(db, enriched_at=OLD)).id,
        event_id=an_event(db).id,
        post_id=(post or a_post(db, fetched_at=OLD)).id,
        attendance="attending",
        included=True,
    )
    db.add(lead)
    db.flush()
    return lead


def forget(db: Session) -> dict[str, int]:
    deleted = retention.forget(db, now=NOW)
    db.commit()
    return deleted


def exists(db: Session, model: type[object], key: object) -> bool:
    return db.get(model, key, populate_existing=True) is not None


def leads_of(db: Session, run: Run) -> int:
    return len(db.scalars(select(Lead).where(Lead.run_id == run.id)).all())


def until_a_round_waits() -> None:
    """Return once another connection waits for a lock: the round under test has reached the point it must wait at,
    so the test needn't guess how long that takes."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with session_scope() as session:
            if session.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted")):
                return
        time.sleep(0.05)
    pytest.fail("the round never waited for a lock")


def test_a_list_is_deleted_when_its_results_expire(db: Session) -> None:
    expired = a_run(db, RunStatus.READY, completed_at=NOW - timedelta(days=30, minutes=1), expires=NOW)
    gone = on_list(db, expired)
    shown = a_run(db, RunStatus.READY, completed_at=NOW - timedelta(days=1), expires=NOW + timedelta(days=29))
    kept = on_list(db, shown)  # its person and post are as old, but a list still shows them
    db.commit()

    deleted = forget(db)

    db.refresh(expired)
    db.refresh(shown)
    assert (expired.status, expired.status_detail, expired.leads_count) == (RunStatus.EXPIRED, retention.EXPIRED, 1)
    assert leads_of(db, expired) == 0
    assert not exists(db, Person, gone.person_id)
    assert not exists(db, Post, gone.post_id)
    assert (shown.status, leads_of(db, shown)) == (RunStatus.READY, 1)
    assert exists(db, Person, kept.person_id)
    assert exists(db, Post, kept.post_id)
    assert (deleted["expired_runs"], deleted["leads"], deleted["people"], deleted["posts"]) == (1, 1, 1, 1)


def test_a_run_that_stopped_loses_its_list_30_days_later(db: Session) -> None:
    failed = a_run(db, RunStatus.FAILED, completed_at=OLD)
    canceled = a_run(db, RunStatus.CANCELED, completed_at=RECENT)
    working = a_run(db, RunStatus.ENRICHING)
    for run in (failed, canceled, working):
        on_list(db, run)
    db.commit()

    forget(db)

    assert [leads_of(db, run) for run in (failed, canceled, working)] == [0, 1, 1]
    db.refresh(failed)
    assert failed.status == RunStatus.FAILED


def test_shared_data_no_list_holds_is_deleted_after_30_days(db: Session) -> None:
    event = an_event(db)
    old_post, recent_post = a_post(db, fetched_at=OLD), a_post(db, fetched_at=RECENT)
    db.add_all(
        [
            EventPost(event_id=event.id, post_id=old_post.id, query="pipeline week"),
            PostLabel(
                post_id=old_post.id, event_id=event.id, label="attending", confidence=Decimal("0.9"), method="rule"
            ),
        ]
    )
    never_looked_up = a_person(db)
    looked_up_long_ago = a_person(db, enriched_at=OLD)
    looked_up_lately = a_person(db, enriched_at=RECENT)  # Apollo's answer is still reused (PERSON_CACHE_DAYS)
    checked_lately = a_person(db, enriched_at=OLD, email_checked_at=RECENT)
    db.commit()

    forget(db)

    assert not exists(db, Post, old_post.id)
    assert db.scalars(select(EventPost).where(EventPost.post_id == old_post.id)).all() == []  # went with the post
    assert db.scalars(select(PostLabel).where(PostLabel.post_id == old_post.id)).all() == []
    assert exists(db, Post, recent_post.id)
    assert not exists(db, Person, never_looked_up.id)
    assert not exists(db, Person, looked_up_long_ago.id)
    assert exists(db, Person, looked_up_lately.id)
    assert exists(db, Person, checked_lately.id)


def test_labels_searches_and_questions_older_than_30_days_are_deleted(db: Session) -> None:
    event, post = an_event(db), a_post(db)
    other_event = an_event(db)
    old_label = PostLabel(
        post_id=post.id, event_id=event.id, label="attending", confidence=Decimal("0.9"), method="rule", created_at=OLD
    )
    recent_label = PostLabel(
        post_id=post.id, event_id=other_event.id, label="speaking", confidence=Decimal("0.8"), method="rule"
    )
    old_search = SearchCache(event_id=event.id, query="pipeline week", fetched_at=OLD)
    recent_search = SearchCache(event_id=event.id, query="#PipelineWeek", fetched_at=RECENT)
    old_question = AiTask(key="a" * 64, kind="label_posts", question={"posts": []}, created_at=OLD)
    old_answer = AiTask(key="b" * 64, kind="analyze_site", question={}, answer={}, created_at=OLD)
    recent_question = AiTask(key="c" * 64, kind="find_events", question={}, created_at=RECENT)
    db.add_all([old_label, recent_label, old_search, recent_search, old_question, old_answer, recent_question])
    db.commit()

    deleted = forget(db)

    assert [label.event_id for label in db.scalars(select(PostLabel))] == [other_event.id]
    assert exists(db, Post, post.id)  # a label going doesn't take its post
    assert [search.id for search in db.scalars(select(SearchCache))] == [recent_search.id]
    assert [task.id for task in db.scalars(select(AiTask))] == [recent_question.id]
    assert (deleted["labels"], deleted["searches"], deleted["ai_tasks"]) == (1, 1, 2)


def test_counters_kept_emails_used_links_and_finished_jobs_are_deleted(db: Session) -> None:
    account = make_account(db)
    hour = NOW.replace(minute=0)
    db.add_all(
        [
            RateLimit(key="sign-up:ip:203.0.113.7", window_start=hour - timedelta(days=2), count=6),
            RateLimit(key="sign-up:ip:203.0.113.7", window_start=hour, count=1),
            OutboxEmail(key="old", to="jo@acme.example", subject="Sign in", text="...", created_at=OLD),
            OutboxEmail(key="new", to="jo@acme.example", subject="Sign in", text="...", created_at=RECENT),
            LoginToken(token_hash="1" * 64, account_id=account.id, expires_at=NOW - timedelta(minutes=1)),
            LoginToken(token_hash="2" * 64, account_id=account.id, expires_at=NOW + timedelta(minutes=29)),
            UserSession(token_hash="3" * 64, account_id=account.id, expires_at=NOW - timedelta(minutes=1)),
            UserSession(token_hash="4" * 64, account_id=account.id, expires_at=NOW + timedelta(days=9), revoked_at=OLD),
            UserSession(token_hash="5" * 64, account_id=account.id, expires_at=NOW + timedelta(days=9)),
            Job(kind="deliver", status=JobStatus.DONE, updated_at=OLD),
            Job(kind="enrich", status=JobStatus.FAILED, updated_at=OLD),
            Job(kind="collect_event", status=JobStatus.QUEUED, updated_at=OLD),
            Job(kind="deliver", status=JobStatus.DONE, updated_at=RECENT),
        ]
    )
    db.commit()

    deleted = forget(db)

    assert [row.window_start for row in db.scalars(select(RateLimit))] == [hour]
    assert [email.key for email in db.scalars(select(OutboxEmail))] == ["new"]
    assert [link.token_hash for link in db.scalars(select(LoginToken))] == ["2" * 64]
    assert [row.token_hash for row in db.scalars(select(UserSession))] == ["5" * 64]
    assert sorted((job.kind, job.status) for job in db.scalars(select(Job))) == [
        ("collect_event", JobStatus.QUEUED),
        ("deliver", JobStatus.DONE),
    ]
    assert (deleted["sign_up_counters"], deleted["kept_emails"], deleted["sign_in_links"]) == (1, 1, 1)
    assert (deleted["sessions"], deleted["jobs"]) == (2, 2)


def test_a_second_round_deletes_nothing(db: Session) -> None:
    on_list(db, a_run(db, RunStatus.READY, completed_at=OLD, expires=NOW - timedelta(days=1)))
    a_person(db, enriched_at=OLD)
    a_post(db, fetched_at=OLD)
    db.commit()
    assert sum(forget(db).values()) > 0
    assert set(forget(db).values()) == {0}


def test_the_database_refuses_to_delete_a_person_or_post_still_on_a_list(db: Session) -> None:
    lead = on_list(db, a_run(db, RunStatus.READY, completed_at=NOW, expires=NOW + timedelta(days=30)))
    db.commit()
    for statement in (delete(Person).where(Person.id == lead.person_id), delete(Post).where(Post.id == lead.post_id)):
        with pytest.raises(IntegrityError):
            db.execute(statement)
        db.rollback()


def test_someone_picked_for_a_list_as_their_data_is_deleted_stays_on_it(db: Session) -> None:
    run = a_run(db, RunStatus.QUALIFYING)
    person, post, event = a_person(db, enriched_at=OLD), a_post(db), an_event(db)  # only the person is old
    db.commit()
    picking = get_sessionmaker()()  # a qualify job adding them to the list, not yet committed
    picking.add(Lead(run_id=run.id, person_id=person.id, event_id=event.id, post_id=post.id, attendance="attending"))
    picking.flush()
    pool = ThreadPoolExecutor(max_workers=1)

    def one_round() -> dict[str, int]:
        with session_scope() as session:
            return retention.forget(session, now=NOW)

    try:
        deleting = pool.submit(one_round)
        until_a_round_waits()  # deleting the person waits for the transaction adding their lead
        assert not deleting.done()
        picking.commit()
        with pytest.raises(IntegrityError):
            deleting.result(timeout=10)  # the lead is in now: the database refuses, and the round is undone
    finally:
        picking.close()
        pool.shutdown()
    assert leads_of(db, run) == 1
    assert forget(db)["people"] == 0  # the next round leaves them be
    assert exists(db, Person, person.id)


def test_two_rounds_never_overlap(db: Session) -> None:
    a_post(db, fetched_at=OLD)
    db.commit()
    first = get_sessionmaker()()
    pool = ThreadPoolExecutor(max_workers=1)

    def second_round() -> dict[str, int]:
        with session_scope() as session:
            return retention.forget(session, now=NOW)

    try:
        assert retention.forget(first, now=NOW)["posts"] == 1
        second = pool.submit(second_round)
        until_a_round_waits()  # for the first round's lock
        assert not second.done()
        first.commit()
        assert second.result(timeout=10)["posts"] == 0
    finally:
        first.close()
        pool.shutdown()


def test_housekeeping_queues_the_job_which_comes_back_every_hour(db: Session, caplog: pytest.LogCaptureFixture) -> None:
    Worker(context(), {}).maintain()  # a worker that doesn't run the job doesn't queue it
    assert db.scalars(select(Job)).all() == []

    worker = Worker(context(), {retention.RETENTION: JOBS[retention.RETENTION]})
    worker.maintain()
    worker.maintain()
    [job] = db.scalars(select(Job)).all()
    assert (job.kind, job.run_after) == (retention.RETENTION, NOW)

    with caplog.at_level(logging.INFO, logger="app.pipeline.retention"):
        assert worker.run_once() is True
    db.refresh(job)
    assert (job.status, job.run_after, job.attempts) == (JobStatus.QUEUED, NOW + retention.EVERY, 0)
    [record] = [r for r in caplog.records if r.getMessage() == "old data deleted"]
    assert all(isinstance(getattr(record, key), int) for key in ("expired_runs", "leads", "people", "posts"))

    worker.maintain()  # one copy waits for the next round; housekeeping doesn't add another
    assert len(db.scalars(select(Job)).all()) == 1


def test_a_round_ends_by_waiting_for_the_next(db: Session) -> None:
    assert retention.retention(context(), db, {}) == Defer(retention.EVERY)
