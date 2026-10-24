"""Enrich and deliver (issue #7): Apollo lookups, the ICP score, the list, the optional email check, the email."""

import dataclasses
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Event, Job, JobStatus, Lead, Person, Post, Run, RunEvent, RunEventStatus, RunStatus
from app.pipeline import enrich
from app.pipeline.base import DONE, JobContext
from app.pipeline.enrich import DELIVER, ENRICH
from app.pipeline.jobs import JOBS
from app.providers.fakes import FakeMailer
from app.providers.registry import build_providers
from app.providers.types import EnrichedPerson, MatchResult, ProviderError
from app.schemas import SiteAnalysis
from app.worker import Worker
from tests.factories import make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ANALYSIS = SiteAnalysis.model_validate(
    {
        "company_name": "Acme Analytics",
        "offering": "Analytics for B2B marketing teams.",
        "icp": {
            "summary": "Marketing leaders at mid-size software companies.",
            "industries": ["Software"],
            "company_sizes": ["51-200", "201-500"],
            "regions": ["United States"],
            "personas": [
                {"label": "Marketing leaders", "title_keywords": ["marketing"], "seniorities": ["vp", "director"]}
            ],
            "exclude_title_keywords": ["recruiter"],
        },
        "event_topics": ["B2B marketing"],
    }
)
_ids = itertools.count(7_100_000_000_000_000_000)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class StubApollo:
    """Apollo knowing the people a test describes (anyone else isn't found), recording each batch it was asked."""

    people: dict[str, EnrichedPerson] = field(default_factory=dict)
    errors: list[ProviderError] = field(default_factory=list)  # raised by the next calls, one each
    calls: list[list[str]] = field(default_factory=list)

    def match(self, linkedin_urls: list[str]) -> MatchResult:
        assert len(linkedin_urls) <= 10
        if self.errors:
            raise self.errors.pop(0)
        self.calls.append(list(linkedin_urls))
        found = [self.people.get(url) for url in linkedin_urls]
        return MatchResult(people=found, credits_used=sum(1 for person in found if person is not None))


@dataclass
class StubVerifier:
    statuses: dict[str, str] = field(default_factory=dict)  # anyone else is "safe"
    errors: list[ProviderError] = field(default_factory=list)  # raised by the next checks, one each
    checked: list[str] = field(default_factory=list)

    def verify(self, email: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        self.checked.append(email)
        return self.statuses.get(email, "safe")


def worker(
    clock: Clock,
    apollo: StubApollo,
    *,
    verifier: StubVerifier | None = None,
    mailer: FakeMailer | None = None,
    **settings: object,
) -> Worker:
    configured = Settings.model_validate(settings)
    providers = dataclasses.replace(
        build_providers(configured), enrichment=apollo, verifier=verifier, mailer=mailer or FakeMailer()
    )
    steps = {ENRICH: JOBS[ENRICH], DELIVER: JOBS[DELIVER]}
    return Worker(JobContext(settings=configured, providers=providers, clock=clock), steps, name="worker-a")


def enriching_run(db: Session, *, event_name: str = "Pipeline Summit") -> tuple[Run, Event]:
    """A run whose candidates qualify just chose, as qualify leaves it."""
    run = make_run(db, status=RunStatus.ENRICHING)
    run.icp = ANALYSIS.model_dump(mode="json")
    summit = Event(key=event_name.lower().replace(" ", "-"), name=event_name)
    db.add(summit)
    db.flush()
    db.add(RunEvent(run_id=run.id, event_id=summit.id, source="suggested", queries=[], status=RunEventStatus.DONE))
    enrich_job = {"run_id": str(run.id)}
    db.add(Job(kind=ENRICH, payload=enrich_job, run_after=NOW, dedupe_key=f"{ENRICH}:{run.id}"))
    db.commit()
    return run, summit


def candidate(
    db: Session,
    run: Run,
    on: Event,
    handle: str,
    *,
    headline: str | None = "Marketing at Northwind",
    attendance: str = "attending",
    exclude_reason: str | None = None,
) -> str:
    """A person qualify kept as a candidate, with the post that showed them at the event. Returns their URL."""
    url = f"https://www.linkedin.com/in/{handle}"
    post_id = str(next(_ids))
    db.add(
        Post(
            id=post_id,
            url=f"https://www.linkedin.com/posts/{handle}-activity-{post_id}",
            author_name=handle.replace("-", " ").title(),
            author_url=url,
            author_headline=headline,
            author_kind="person",
            text="See you at Pipeline Summit!",
            posted_at=NOW,
        )
    )
    person = db.scalar(select(Person).where(Person.linkedin_url == url))
    if person is None:
        person = Person(linkedin_url=url, name=handle.replace("-", " ").title(), headline=headline)
        db.add(person)
    db.flush()
    db.add(
        Lead(
            run_id=run.id,
            person_id=person.id,
            event_id=on.id,
            post_id=post_id,
            attendance=attendance,
            exclude_reason=exclude_reason,
        )
    )
    db.commit()
    return url


def found(
    url: str,
    *,
    title: str | None = "VP Marketing",
    seniority: str | None = "vp",
    email: str | None = None,
    email_status: str | None = "verified",
    industry: str | None = "computer software",
    size: int | None = 120,
    company_domain: str | None = "northwind.example",
) -> EnrichedPerson:
    handle = url.rsplit("/", 1)[-1]
    return EnrichedPerson(
        linkedin_url=url,
        apollo_id=f"apollo-{handle}",
        name=handle.replace("-", " ").title(),
        headline=None,
        title=title,
        seniority=seniority,
        departments=["master_marketing"],
        email=email if email is not None else f"{handle}@northwind.example",
        email_status=email_status,
        email_domain_catchall=False,
        company_name="Northwind Labs",
        company_domain=company_domain,
        company_industry=industry,
        company_size=size,
        country="United States",
    )


def run_jobs(db: Session, w: Worker, clock: Clock, *, max_steps: int = 60) -> None:
    """Run the worker until no enrich or deliver job is waiting, moving the clock to each deferred job's time."""
    for _ in range(max_steps):
        if w.run_once():
            continue
        db.expire_all()
        due = db.scalar(
            select(func.min(Job.run_after)).where(Job.kind.in_([ENRICH, DELIVER]), Job.status == JobStatus.QUEUED)
        )
        if due is None:
            return
        clock.now = max(clock.now, due)
    raise AssertionError("enrich jobs are still waiting")


def fresh(db: Session, run: Run) -> Run:
    db.expire_all()
    found_run = db.get(Run, run.id)
    assert found_run is not None
    return found_run


def leads(db: Session, run: Run) -> dict[str, Lead]:
    """The run's leads by the person's LinkedIn handle."""
    db.expire_all()
    rows = db.execute(select(Lead, Person.linkedin_url).join(Person).where(Lead.run_id == run.id))
    return {url.rsplit("/", 1)[-1]: lead for lead, url in rows}


def included(db: Session, run: Run) -> set[str]:
    return {handle for handle, lead in leads(db, run).items() if lead.included}


def reasons(db: Session, run: Run) -> dict[str, str | None]:
    return {handle: lead.exclude_reason for handle, lead in leads(db, run).items() if not lead.included}


def person(db: Session, url: str) -> Person:
    db.expire_all()
    row = db.scalar(select(Person).where(Person.linkedin_url == url))
    assert row is not None
    return row


def jobs(db: Session, kind: str) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == kind).order_by(Job.id)))


# ------------------------------------------------------------------------------------------------------- enrich
def test_candidates_are_looked_up_ten_at_a_time_and_those_who_fit_make_the_list(db: Session) -> None:
    run, summit = enriching_run(db)
    urls = [candidate(db, run, summit, f"person-{i:02d}") for i in range(23)]
    apollo = StubApollo({url: found(url) for url in urls})
    clock = Clock()
    run_jobs(db, worker(clock, apollo), clock)

    assert [len(batch) for batch in apollo.calls] == [10, 10, 3]
    assert sorted(itertools.chain(*apollo.calls)) == sorted(urls)  # everyone asked about once
    ready = fresh(db, run)
    assert (ready.status, ready.status_detail, ready.leads_count) == (RunStatus.READY, enrich.READY, 23)
    assert ready.apollo_credits_used == 23
    assert ready.completed_at is not None
    assert ready.results_expire_at == ready.completed_at + timedelta(days=30)
    lead = leads(db, run)["person-00"]
    assert (lead.included, lead.icp_score, lead.exclude_reason) == (True, 100, None)
    assert lead.icp_reasons == [
        "title matches 'marketing'",
        "seniority is vp",
        "industry matches 'Software'",
        "company size is 51-200",
    ]
    looked_up = person(db, urls[0])
    assert (looked_up.email, looked_up.email_status, looked_up.title) == (
        "person-00@northwind.example",
        "verified",
        "VP Marketing",
    )
    assert looked_up.enriched_at == NOW


def test_people_looked_up_lately_are_not_asked_about_again(db: Session) -> None:
    run, summit = enriching_run(db)
    recent, old = candidate(db, run, summit, "recent"), candidate(db, run, summit, "old")
    for url, when in [(recent, NOW - timedelta(days=10)), (old, NOW - timedelta(days=31))]:
        row = person(db, url)
        row.enriched_at, row.apollo_id, row.title, row.seniority = when, "apollo-x", "VP Marketing", "vp"
        row.email, row.email_status = f"{url.rsplit('/', 1)[-1]}@northwind.example", "verified"
        db.commit()  # before the next person() call expires what isn't saved
    apollo = StubApollo({old: found(old)})
    clock = Clock()
    run_jobs(db, worker(clock, apollo), clock)

    assert apollo.calls == [[old]]  # the 31-day-old data is past PERSON_CACHE_DAYS, so it's asked for again
    assert included(db, run) == {"recent", "old"}
    assert fresh(db, run).apollo_credits_used == 1


def test_someone_apollo_did_not_find_is_remembered_too(db: Session) -> None:
    first, summit = enriching_run(db)
    url = candidate(db, first, summit, "nobody-knows")
    apollo, clock = StubApollo(), Clock()
    run_jobs(db, worker(clock, apollo), clock)
    assert reasons(db, first) == {"nobody-knows": "not found in Apollo"}

    second = make_run(db, status=RunStatus.ENRICHING)
    second.icp = ANALYSIS.model_dump(mode="json")
    db.add(Job(kind=ENRICH, payload={"run_id": str(second.id)}, run_after=clock.now))
    db.commit()
    candidate(db, second, summit, "nobody-knows")
    run_jobs(db, worker(clock, apollo), clock)

    assert apollo.calls == [[url]]  # the second run didn't ask again
    assert reasons(db, second) == {"nobody-knows": "not found in Apollo"}


def test_only_emails_apollo_marks_verified_are_kept(db: Session) -> None:
    run, summit = enriching_run(db)
    statuses = {"verified": "verified", "unverified": "unverified", "guessed": "extrapolated"}
    urls = {handle: candidate(db, run, summit, handle) for handle in [*statuses, "no-email"]}
    people = {urls[handle]: found(urls[handle], email_status=status) for handle, status in statuses.items()}
    people[urls["no-email"]] = found(urls["no-email"], email="", email_status=None)
    clock = Clock()
    run_jobs(db, worker(clock, StubApollo(people)), clock)

    assert included(db, run) == {"verified"}
    assert reasons(db, run) == {
        "unverified": "no verified email",
        "guessed": "no verified email",
        "no-email": "no verified email",
    }


def test_the_accepted_email_statuses_can_be_widened(db: Session) -> None:
    run, summit = enriching_run(db)
    url = candidate(db, run, summit, "likely")
    clock = Clock()
    apollo = StubApollo({url: found(url, email_status="Likely To Engage")})
    run_jobs(db, worker(clock, apollo, accepted_email_statuses="verified, likely to engage"), clock)
    assert included(db, run) == {"likely"}


def test_a_run_uses_no_more_apollo_credits_than_it_is_allowed(db: Session) -> None:
    run, summit = enriching_run(db)
    urls = [candidate(db, run, summit, f"person-{i:02d}") for i in range(25)]
    apollo = StubApollo({url: found(url) for url in urls})
    clock = Clock()
    run_jobs(db, worker(clock, apollo, apollo_credits_per_run=15), clock)

    assert [len(batch) for batch in apollo.calls] == [10, 5]
    ready = fresh(db, run)
    assert (ready.apollo_credits_used, ready.leads_count) == (15, 15)
    assert set(reasons(db, run).values()) == {"not looked up: the run's Apollo credits ran out"}


def test_candidates_whose_headline_fits_are_looked_up_first(db: Session) -> None:
    run, summit = enriching_run(db)
    engineers = [candidate(db, run, summit, f"engineer-{i}", headline="Software Engineer") for i in range(3)]
    marketers = [candidate(db, run, summit, f"marketer-{i}", headline="Head of Marketing") for i in range(3)]
    apollo = StubApollo({url: found(url) for url in engineers + marketers})
    clock = Clock()
    run_jobs(db, worker(clock, apollo, apollo_credits_per_run=3), clock)
    assert sorted(apollo.calls[0]) == sorted(marketers)


def test_the_list_is_the_best_by_score_then_the_most_telling_post(db: Session) -> None:
    run, summit = enriching_run(db)
    people: dict[str, tuple[dict[str, Any], str]] = {
        "perfect": ({"title": "VP Marketing"}, "attended"),  # 100
        "manager": ({"title": "Marketing Manager", "seniority": "manager", "size": 4000}, "speaking"),  # 60
        "speaker": ({"title": "Marketing Lead", "seniority": "senior", "size": 9000}, "speaking"),  # 60
        "attendee": ({"title": "Marketing Lead", "seniority": "senior", "size": 9000}, "attended"),  # 60
    }
    apollo = StubApollo()
    for handle, (fields, attendance) in people.items():
        url = candidate(db, run, summit, handle, attendance=attendance)
        apollo.people[url] = found(url, **fields)
    clock = Clock()
    run_jobs(db, worker(clock, apollo, max_leads=3), clock)

    scored = leads(db, run)
    assert {handle: lead.icp_score for handle, lead in scored.items()} == {
        "perfect": 100,
        "manager": 60,
        "speaker": 60,
        "attendee": 60,
    }
    assert included(db, run) == {"perfect", "manager", "speaker"}  # ties go to speakers before attendees
    assert reasons(db, run) == {"attendee": "ranked below the best 3"}
    assert fresh(db, run).leads_count == 3


def test_people_who_do_not_fit_are_left_out_with_the_reason(db: Session) -> None:
    run, summit = enriching_run(db)
    profiles: dict[str, dict[str, Any]] = {
        "fits": {},
        "engineer": {"title": "Software Engineer", "seniority": "senior"},  # 35: industry and size only
        "recruiter": {"title": "Marketing Recruiter"},
        "colleague": {"company_domain": "acme.example"},  # the requester's own company
        "colleague-by-email": {"company_domain": None, "email": "someone@www.acme.example"},
    }
    apollo = StubApollo()
    for handle, fields in profiles.items():
        url = candidate(db, run, summit, handle)
        apollo.people[url] = found(url, **fields)
    clock = Clock()
    run_jobs(db, worker(clock, apollo), clock)

    assert included(db, run) == {"fits"}
    assert reasons(db, run) == {
        "engineer": "ICP score 35 is below 40",
        "recruiter": "title says 'recruiter'",
        "colleague": "works at the requester's company",
        "colleague-by-email": "works at the requester's company",
    }
    assert leads(db, run)["engineer"].icp_reasons == ["industry matches 'Software'", "company size is 51-200"]


def test_two_profiles_with_one_email_are_listed_once(db: Session) -> None:
    run, summit = enriching_run(db)
    first, second = candidate(db, run, summit, "jane-a"), candidate(db, run, summit, "jane-b")
    apollo = StubApollo({url: found(url, email="Jane@Northwind.example") for url in (first, second)})
    clock = Clock()
    run_jobs(db, worker(clock, apollo), clock)
    assert len(included(db, run)) == 1
    assert list(reasons(db, run).values()) == ["same email as a lead ranked higher"]


def test_candidates_qualify_screened_out_are_not_looked_up(db: Session) -> None:
    run, summit = enriching_run(db)
    candidate(db, run, summit, "student", headline="Marketing student", exclude_reason="headline says 'student'")
    apollo, clock = StubApollo(), Clock()
    run_jobs(db, worker(clock, apollo), clock)

    assert apollo.calls == []
    assert reasons(db, run) == {"student": "headline says 'student'"}  # qualify's reason stands
    ready = fresh(db, run)
    assert (ready.status, ready.status_detail, ready.leads_count) == (RunStatus.READY, enrich.NOTHING_FOUND, 0)


def test_the_job_takes_turns_with_other_runs(db: Session) -> None:
    run, summit = enriching_run(db)
    urls = [candidate(db, run, summit, f"person-{i:02d}") for i in range(60)]
    apollo = StubApollo({url: found(url) for url in urls})
    clock = Clock()
    w = worker(clock, apollo)
    assert w.run_once()

    assert len(apollo.calls) == enrich.CALLS_PER_TURN  # then it went to the back of the queue
    [job] = jobs(db, ENRICH)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)
    assert fresh(db, run).status == RunStatus.ENRICHING
    run_jobs(db, w, clock)
    assert len(apollo.calls) == 6
    assert fresh(db, run).leads_count == 50  # MAX_LEADS


def test_an_answer_overtaken_by_a_newer_one_does_not_replace_it(db: Session) -> None:
    run, summit = enriching_run(db)
    url = candidate(db, run, summit, "in-two-runs")
    clock = Clock()

    class Overtaken(StubApollo):
        def match(self, linkedin_urls: list[str]) -> MatchResult:
            answer = super().match(linkedin_urls)
            clock.now += timedelta(seconds=5)  # Apollo took its time
            # Meanwhile another run asked about the same person, after this one did, and saved its answer first.
            newer = enrich._apollo_fields(found(url, email="newer@northwind.example"), NOW + timedelta(seconds=2))
            db.execute(update(Person).where(Person.linkedin_url == url).values(**newer))
            db.commit()
            return answer

    run_jobs(db, worker(clock, Overtaken({url: found(url)})), clock)

    saved = person(db, url)
    assert (saved.email, saved.enriched_at) == ("newer@northwind.example", NOW + timedelta(seconds=2))
    assert included(db, run) == {"in-two-runs"}
    assert fresh(db, run).apollo_credits_used == 1  # the overtaken answer was paid for all the same


def test_a_busy_apollo_is_asked_again_later(db: Session) -> None:
    run, summit = enriching_run(db)
    url = candidate(db, run, summit, "patient")
    apollo = StubApollo({url: found(url)}, errors=[ProviderError("Apollo is busy or failing (503)", retryable=True)])
    clock = Clock()
    run_jobs(db, worker(clock, apollo), clock)
    assert apollo.calls == [[url]]
    assert fresh(db, run).status == RunStatus.READY


def test_apollo_refusing_the_request_fails_the_run(db: Session) -> None:
    run, summit = enriching_run(db)
    candidate(db, run, summit, "unlucky")
    refused = ProviderError("Apollo rejected our credentials (401)", retryable=False)
    clock = Clock()
    run_jobs(db, worker(clock, StubApollo(errors=[refused])), clock)

    failed = fresh(db, run)
    assert (failed.status, failed.status_detail) == (RunStatus.FAILED, enrich.SOMETHING_WRONG)
    assert jobs(db, DELIVER) == []


def test_a_run_no_longer_enriching_is_left_alone(db: Session) -> None:
    run, summit = enriching_run(db)
    candidate(db, run, summit, "anyone")
    fresh(db, run).status = RunStatus.CANCELED
    db.commit()
    apollo, clock = StubApollo(), Clock()
    run_jobs(db, worker(clock, apollo), clock)
    assert apollo.calls == []
    assert fresh(db, run).status == RunStatus.CANCELED


def test_the_jobs_are_registered() -> None:
    assert JOBS[ENRICH].on_give_up is enrich.give_up
    assert JOBS[DELIVER].on_give_up is enrich.give_up_delivery


# ------------------------------------------------------------------------------------------- the email check
def four_who_fit(db: Session, run: Run, summit: Event) -> StubApollo:
    """Four people on the list, best first: Ann (100), Bob (85), Cat (75), Dan (60)."""
    profiles: dict[str, dict[str, Any]] = {
        "ann": {},
        "bob": {"size": 4000},
        "cat": {"seniority": "manager"},
        "dan": {"seniority": "manager", "size": 4000},
    }
    apollo = StubApollo()
    for handle, fields in profiles.items():
        url = candidate(db, run, summit, handle)
        apollo.people[url] = found(url, **fields)
    return apollo


def test_the_email_check_takes_off_only_invalid_disabled_and_spam_trap_addresses(db: Session) -> None:
    run, summit = enriching_run(db)
    apollo = four_who_fit(db, run, summit)
    verifier = StubVerifier(
        {
            "ann@northwind.example": "invalid",
            "bob@northwind.example": "disabled",
            "cat@northwind.example": "spamtrap",
            "dan@northwind.example": "catch_all",  # unsure, not wrong: it stays
        }
    )
    clock = Clock()
    run_jobs(db, worker(clock, apollo, verifier=verifier), clock)

    assert sorted(verifier.checked) == [f"{name}@northwind.example" for name in ["ann", "bob", "cat", "dan"]]
    assert included(db, run) == {"dan"}
    assert reasons(db, run) == {
        "ann": "the email check says invalid",
        "bob": "the email check says disabled",
        "cat": "the email check says spamtrap",
    }
    checked = person(db, "https://www.linkedin.com/in/dan")
    assert (checked.email_check, checked.email_checked_at) == ("catch_all", NOW)


def test_the_list_is_not_refilled_after_the_check(db: Session) -> None:
    run, summit = enriching_run(db)
    apollo = four_who_fit(db, run, summit)
    verifier = StubVerifier({"ann@northwind.example": "invalid"})
    clock = Clock()
    run_jobs(db, worker(clock, apollo, verifier=verifier, max_leads=2), clock)

    assert sorted(verifier.checked) == ["ann@northwind.example", "bob@northwind.example"]  # only the best two
    assert included(db, run) == {"bob"}
    assert reasons(db, run) == {
        "ann": "the email check says invalid",
        "cat": "ranked below the best 2",
        "dan": "ranked below the best 2",
    }
    assert fresh(db, run).leads_count == 1


def test_a_recent_check_of_the_same_address_is_reused(db: Session) -> None:
    run, summit = enriching_run(db)
    url = candidate(db, run, summit, "checked")
    row = person(db, url)
    row.enriched_at, row.apollo_id, row.title, row.seniority = NOW - timedelta(days=1), "apollo-x", "VP Marketing", "vp"
    row.email, row.email_status = "checked@northwind.example", "verified"
    row.email_check, row.email_checked_at = "safe", NOW - timedelta(days=1)
    db.commit()
    verifier, clock = StubVerifier(), Clock()
    run_jobs(db, worker(clock, StubApollo(), verifier=verifier), clock)
    assert verifier.checked == []
    assert included(db, run) == {"checked"}


def test_without_a_working_email_check_the_list_goes_out_on_apollo_alone(db: Session) -> None:
    run, summit = enriching_run(db)
    apollo = four_who_fit(db, run, summit)
    verifier = StubVerifier(errors=[ProviderError("Reoon rejected our credentials (401)", retryable=False)])
    clock = Clock()
    run_jobs(db, worker(clock, apollo, verifier=verifier), clock)

    ready = fresh(db, run)
    assert (ready.status, ready.leads_count) == (RunStatus.READY, 4)
    assert ready.error == "email check skipped: Reoon rejected our credentials (401)"


def test_a_busy_email_check_is_tried_again_later(db: Session) -> None:
    run, summit = enriching_run(db)
    apollo = four_who_fit(db, run, summit)
    verifier = StubVerifier(errors=[ProviderError("Reoon is busy or failing (503)", retryable=True)])
    clock = Clock()
    run_jobs(db, worker(clock, apollo, verifier=verifier), clock)
    assert len(verifier.checked) == 4
    ready = fresh(db, run)
    assert (ready.leads_count, ready.error) == (4, None)


def test_there_is_no_email_check_unless_it_is_turned_on(db: Session) -> None:
    run, summit = enriching_run(db)
    url = candidate(db, run, summit, "unchecked")
    clock = Clock()
    run_jobs(db, worker(clock, StubApollo({url: found(url)})), clock)
    assert person(db, url).email_checked_at is None
    assert included(db, run) == {"unchecked"}


# ------------------------------------------------------------------------------------------------------ deliver
def requester(db: Session, run: Run) -> str:
    account = db.get(Account, run.account_id)
    assert account is not None
    return account.email


def test_the_requester_is_emailed_the_link_once(db: Session) -> None:
    run, summit = enriching_run(db)
    urls = [candidate(db, run, summit, handle) for handle in ["ann", "bob"]]
    mailer, clock = FakeMailer(), Clock()
    w = worker(clock, StubApollo({url: found(url) for url in urls}), mailer=mailer, base_url="https://leads.example/")
    run_jobs(db, w, clock)

    [sent] = mailer.outbox
    assert (sent.to, sent.key, sent.subject) == (requester(db, run), f"results:{run.id}", "Your event leads are ready")
    assert "We found 2 people who went to, or are going to, Pipeline Summit" in sent.text
    assert f"https://leads.example/runs/{run.id}\n" in sent.text
    assert "available for 30 days" in sent.text

    [job] = jobs(db, DELIVER)
    assert job.status == JobStatus.DONE
    assert enrich.deliver(w.ctx, db, job.payload) is DONE  # sent again, as after a crash...
    db.commit()
    assert len(mailer.outbox) == 1  # ...and dropped by its key


def test_a_run_that_found_nobody_says_so(db: Session) -> None:
    enriching_run(db)
    mailer, clock = FakeMailer(), Clock()
    run_jobs(db, worker(clock, StubApollo(), mailer=mailer), clock)
    [sent] = mailer.outbox
    assert sent.subject == "Your event leads search has finished"
    assert "found nobody who matches your ICP" in sent.text


def test_no_email_for_a_run_canceled_before_it_went_out(db: Session) -> None:
    run, _ = enriching_run(db)
    mailer, clock = FakeMailer(), Clock()
    w = worker(clock, StubApollo(), mailer=mailer)
    assert w.run_once()  # enrich: the run is ready and the email queued
    fresh(db, run).status = RunStatus.CANCELED
    db.commit()
    run_jobs(db, w, clock)
    assert mailer.outbox == []


def test_an_email_that_can_no_longer_go_out_just_once_is_not_sent(db: Session) -> None:
    run, _ = enriching_run(db)
    mailer, clock = FakeMailer(), Clock()
    w = worker(clock, StubApollo(), mailer=mailer)
    assert w.run_once()
    clock.now += timedelta(hours=24)  # the worker was down all day: Resend no longer remembers the key
    run_jobs(db, w, clock)
    assert mailer.outbox == []
    assert fresh(db, run).error == "results email not sent: it could no longer be sent safely just once"


def test_an_email_that_fails_for_good_is_noted_and_the_run_stays_ready(db: Session) -> None:
    class RefusingMailer(FakeMailer):
        def send(self, _email: object) -> None:
            raise ProviderError("Resend refused the request (422)", retryable=False)

    run, _ = enriching_run(db)
    clock = Clock()
    run_jobs(db, worker(clock, StubApollo(), mailer=RefusingMailer()), clock)
    done = fresh(db, run)
    assert (done.status, done.error) == (RunStatus.READY, "results email not sent")
    [job] = jobs(db, DELIVER)
    assert job.status == JobStatus.FAILED


def test_a_note_from_an_earlier_step_is_kept(db: Session) -> None:
    run, summit = enriching_run(db)
    fresh(db, run).error = "the run's budget is used up: go on with the posts already labeled"
    db.commit()
    apollo = four_who_fit(db, run, summit)
    verifier = StubVerifier(errors=[ProviderError("Reoon rejected our credentials (401)", retryable=False)])
    clock = Clock()
    run_jobs(db, worker(clock, apollo, verifier=verifier), clock)
    assert fresh(db, run).error == (
        "the run's budget is used up: go on with the posts already labeled; "
        "email check skipped: Reoon rejected our credentials (401)"
    )
