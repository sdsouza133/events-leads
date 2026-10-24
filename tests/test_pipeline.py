"""The whole pipeline after approval, on the offline fakes: every step hands over to the next, and it ends with the
run's list and one email to the requester."""

import dataclasses
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Job, JobStatus, Lead, Run, RunStatus
from app.pipeline.base import JobContext
from app.pipeline.collect import approve
from app.pipeline.jobs import JOBS
from app.providers.fakes import FakeLlm, FakeMailer
from app.providers.registry import build_providers
from app.providers.types import FetchedPage
from app.schemas import ApprovedEvent
from app.worker import Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_an_approved_run_ends_with_its_list_and_one_email(db: Session) -> None:
    home = FetchedPage("https://acme.example/", 200, "Acme Analytics | Home", None, "")
    run = make_run(db, status=RunStatus.AWAITING_APPROVAL)
    run.icp = FakeLlm().analyze_site([home]).value.model_dump(mode="json")
    summit = ApprovedEvent.model_validate(
        {"name": "Pipeline Summit", "queries": ['"Pipeline Summit"', "#PipelineSummit"]}
    )
    assert approve(db, run.id, [summit], now=NOW)
    db.commit()

    clock, mailer, settings = Clock(), FakeMailer(), Settings()
    providers = dataclasses.replace(build_providers(settings), mailer=mailer)
    w = Worker(JobContext(settings=settings, providers=providers, clock=clock), JOBS, name="worker-a")
    for _ in range(200):
        if w.run_once():
            continue
        db.expire_all()
        due = db.scalar(select(func.min(Job.run_after)).where(Job.status == JobStatus.QUEUED))
        if due is None:
            break
        clock.now = max(clock.now, due)  # a search to poll again, or the next turn of a job
    else:
        raise AssertionError("jobs are still waiting")

    db.expire_all()
    ready = db.get(Run, run.id)
    assert ready is not None
    assert (ready.status, ready.error) == (RunStatus.READY, None)
    listed = list(db.scalars(select(Lead).where(Lead.run_id == run.id, Lead.included)))
    assert 0 < len(listed) == ready.leads_count <= settings.max_leads
    assert all(lead.icp_score >= settings.min_icp_score and lead.icp_reasons for lead in listed)
    assert {job.status for job in db.scalars(select(Job))} == {JobStatus.DONE}
    [sent] = mailer.outbox
    assert sent.key == f"results:{run.id}"
