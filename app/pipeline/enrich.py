"""Enrich and deliver (issue #7): a verified work email and an ICP score for each candidate, then the run's list.

1. Apollo looks up the candidates qualify kept (those without an exclude reason), ten LinkedIn URLs per call,
   likeliest fits first. Its answer, found or not, is kept on the shared person row, so nobody is asked about twice
   within PERSON_CACHE_DAYS. Apollo bills a credit per person found; a run uses at most APOLLO_CREDITS_PER_RUN.
2. Each person found is scored against the ICP (`app/pipeline/scoring.py`), and left out, with the reason, without
   an email Apollo marks verified (ACCEPTED_EMAIL_STATUSES), with a title the ICP excludes, at the requester's
   company, or with a score below MIN_ICP_SCORE.
3. The best MAX_LEADS make the list. With Reoon on, their addresses get a second check, which takes off only those
   it calls invalid, disabled or a spam trap. The list isn't refilled, so a run checks at most MAX_LEADS addresses.
4. The run is ready, and a `deliver` job emails the requester the link, once (ADR 0002).

Like qualify, the job makes a few calls per turn and then defers to itself, so other runs get a turn.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import emails, queue
from app.models import Account, Event, Lead, Person, Run, RunEvent, RunEventStatus, RunStatus
from app.pipeline import rules, runs, scoring
from app.pipeline.base import DONE, Defer, JobContext, Outcome, Payload
from app.providers.protocols import EmailVerifier
from app.providers.types import EnrichedPerson, ProviderError
from app.schemas import MOST_TELLING, AttendanceLabel, Icp, SiteAnalysis

log = logging.getLogger(__name__)

ENRICH = "enrich"
DELIVER = "deliver"
LOOKUP_BATCH = 10  # Apollo's bulk match limit
CALLS_PER_TURN = 5
NEXT_TURN = timedelta(0)  # back of the queue, at once
DROPPED_BY_CHECK = frozenset({"invalid", "disabled", "spamtrap"})
SEND_WINDOW = timedelta(hours=23)  # Resend drops a repeated key for 24 hours; later, a retry could send twice

READY = "Your list is ready."
NOTHING_FOUND = "We didn't find anyone at these events who matches your ICP and has a verified work email."
SOMETHING_WRONG = "Something went wrong while finding emails. We've been notified."


@dataclass
class _Verdict:
    lead: Lead
    person: Person
    points: int = 0
    reasons: list[str] = field(default_factory=list)
    out: str | None = None  # why they aren't on the list; None while they could be


def enrich(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    run_id = uuid.UUID(payload["run_id"])
    verifier = ctx.providers.verifier
    for _ in range(CALLS_PER_TURN):
        run = session.get(Run, run_id, populate_existing=True)
        if run is None or run.status != RunStatus.ENRICHING or run.icp is None:
            return DONE
        icp = SiteAnalysis.model_validate(run.icp).icp
        candidates = _candidates(session, run_id)
        lookups = _to_look_up(ctx, run, icp, candidates)
        if lookups:
            _look_up(ctx, session, run_id, lookups)
            continue
        if verifier is not None:
            _, shortlist = _shortlist(ctx, run, icp, candidates)
            unchecked = next((v for v in shortlist if not _fresh(ctx, v.person.email_checked_at)), None)
            if unchecked is not None:
                skipped = _check(ctx, session, run_id=run_id, verifier=verifier, verdict=unchecked)
                if skipped is not None:
                    return _finish(ctx, session, run_id, note=skipped)
                continue
        return _finish(ctx, session, run_id)
    return Defer(NEXT_TURN)


def give_up(ctx: JobContext, session: Session, payload: Payload) -> None:
    """The job failed for good (Apollo rejected the key, say): fail the run."""
    run = runs.locked(session, uuid.UUID(payload["run_id"]))
    if run is not None and run.status == RunStatus.ENRICHING:
        runs.fail(run, SOMETHING_WRONG, now=ctx.now(), error="enrich job failed after its last attempt")


def deliver(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    """Email the requester the link to their finished run."""
    run_id = uuid.UUID(payload["run_id"])
    run = session.get(Run, run_id)
    if run is None or run.status != RunStatus.READY:
        return DONE  # canceled or expired since: nothing to send
    if ctx.now() - datetime.fromisoformat(payload["queued_at"]) > SEND_WINDOW:
        # Resend drops a repeated key for 24 hours only: past that, a retry could reach the requester twice.
        _note(run, "results email not sent: it could no longer be sent safely just once")
        log.warning("results email not sent: too late to send it safely", extra={"run_id": str(run_id)})
        return DONE
    account = session.get(Account, run.account_id)
    if account is None:
        return DONE
    message = emails.results_email(
        to=account.email,
        base_url=ctx.settings.base_url,
        run_id=run_id,
        leads=run.leads_count,
        events=_event_names(session, run_id),
        days=ctx.settings.results_days,
    )
    session.commit()  # nothing is held open while Resend answers
    ctx.providers.mailer.send(message)
    log.info("results email sent", extra={"run_id": str(run_id)})
    return DONE


def give_up_delivery(_ctx: JobContext, session: Session, payload: Payload) -> None:
    """The email couldn't be sent. The results are there all the same, so only note it for the operator."""
    run = session.get(Run, uuid.UUID(payload["run_id"]), with_for_update=True)
    if run is not None:
        _note(run, "results email not sent")


def _candidates(session: Session, run_id: uuid.UUID) -> list[tuple[Lead, Person]]:
    """The run's candidates still in the running (qualify's pre-screen left the others out), as stored now."""
    rows = session.execute(
        select(Lead, Person)
        .join(Person, Person.id == Lead.person_id)
        .where(Lead.run_id == run_id, Lead.exclude_reason.is_(None))
        .order_by(Person.id)
        .execution_options(populate_existing=True)
    )
    return [(lead, person) for lead, person in rows]


def _to_look_up(ctx: JobContext, run: Run, icp: Icp, candidates: list[tuple[Lead, Person]]) -> list[Person]:
    """The next people to ask Apollo about: not looked up lately, likeliest fits first, within the run's credits.

    Each person asked costs at most one credit, so a batch never takes the run past its allowance.
    """
    allowance = ctx.settings.apollo_credits_per_run - run.apollo_credits_used
    if allowance <= 0:
        return []
    stale = [(lead, person) for lead, person in candidates if not _fresh(ctx, person.enriched_at)]
    stale.sort(key=lambda pair: (not scoring.fits_a_persona(pair[1].headline, icp), _telling(pair[0]), pair[1].id))
    return [person for _, person in stale[: min(LOOKUP_BATCH, allowance)]]


def _look_up(ctx: JobContext, session: Session, run_id: uuid.UUID, people: list[Person]) -> None:
    asked = [(person.id, person.linkedin_url) for person in people]
    session.commit()  # nothing is held open while Apollo answers
    found = ctx.providers.enrichment.match([url for _, url in asked])
    now = ctx.now()
    run = runs.locked(session, run_id)  # the run, then its people: the order every step locks them in
    if run is not None:
        run.apollo_credits_used += found.credits_used
    answers = sorted(zip((person_id for person_id, _ in asked), found.people, strict=True), key=lambda a: a[0])
    for person_id, match in answers:  # people in a fixed order, so two runs updating the same ones can't deadlock
        session.execute(update(Person).where(Person.id == person_id).values(**_apollo_fields(match, now)))
    session.commit()


def _apollo_fields(match: EnrichedPerson | None, now: datetime) -> dict[str, Any]:
    """What Apollo says now, replacing what it said before. An earlier email check goes with the earlier address."""
    fields: dict[str, Any] = {
        "enriched_at": now,
        "apollo_id": None,
        "title": None,
        "seniority": None,
        "departments": [],
        "company_name": None,
        "company_domain": None,
        "company_industry": None,
        "company_size": None,
        "country": None,
        "email": None,
        "email_status": None,
        "email_check": None,
        "email_checked_at": None,
    }
    if match is None:
        return fields
    email = match.email if match.email and len(match.email) <= 320 else None  # noqa: PLR2004 (the column's size)
    fields.update(
        apollo_id=_cut(match.apollo_id, 64),
        title=_cut(match.title, 300),
        seniority=_cut(match.seniority, 32),
        departments=list(match.departments),
        company_name=_cut(match.company_name, 300),
        company_domain=_cut(match.company_domain, 253),
        company_industry=_cut(match.company_industry, 200),
        company_size=match.company_size,
        country=_cut(match.country, 100),
        email=email,
        email_status=_cut(match.email_status, 32),
    )
    if match.name:
        fields["name"] = _cut(match.name, 300)
    return fields


def _shortlist(
    ctx: JobContext, run: Run, icp: Icp, candidates: list[tuple[Lead, Person]]
) -> tuple[list[_Verdict], list[_Verdict]]:
    """Every candidate's verdict, and the best MAX_LEADS of those still in, best first, before any email check."""
    verdicts = [_judge(ctx, run, icp, lead, person) for lead, person in candidates]
    best_first = sorted(
        (v for v in verdicts if v.out is None), key=lambda v: (-v.points, _telling(v.lead), v.person.id)
    )
    shortlist: list[_Verdict] = []
    emails_listed: set[str] = set()
    for verdict in best_first:
        email = (verdict.person.email or "").casefold()
        if email in emails_listed:
            verdict.out = "same email as a lead ranked higher"
        elif len(shortlist) == ctx.settings.max_leads:
            verdict.out = f"ranked below the best {ctx.settings.max_leads}"
        else:
            emails_listed.add(email)
            shortlist.append(verdict)
    return verdicts, shortlist


def _judge(ctx: JobContext, run: Run, icp: Icp, lead: Lead, person: Person) -> _Verdict:
    verdict = _Verdict(lead, person)
    if not _fresh(ctx, person.enriched_at):
        verdict.out = "not looked up: the run's Apollo credits ran out"
        return verdict
    if person.apollo_id is None:
        verdict.out = "not found in Apollo"
        return verdict
    result = scoring.score(_profile(person), icp)
    verdict.points, verdict.reasons = result.points, result.reasons
    if not person.email or (person.email_status or "").lower() not in ctx.settings.accepted_email_statuses:
        verdict.out = "no verified email"
    elif (why := rules.prescreen(person.title, icp, what="title")) is not None:
        verdict.out = why
    elif _same_company(person, run.domain):
        verdict.out = "works at the requester's company"
    elif result.points < ctx.settings.min_icp_score:
        verdict.out = f"ICP score {result.points} is below {ctx.settings.min_icp_score}"
    return verdict


def _check(
    ctx: JobContext, session: Session, *, run_id: uuid.UUID, verifier: EmailVerifier, verdict: _Verdict
) -> str | None:
    """Reoon's word on one address, kept on the person. If Reoon refuses (a rejected key), a note saying the check
    was skipped: Apollo already marked these addresses verified, so the list goes out without it."""
    person_id, email = verdict.person.id, verdict.person.email or ""
    session.commit()  # nothing is held open while Reoon answers
    try:
        status = verifier.verify(email)
    except ProviderError as exc:
        if exc.retryable:
            raise
        log.warning("email check skipped", extra={"run_id": str(run_id), "error": str(exc)})
        return f"email check skipped: {exc}"
    session.execute(
        update(Person)
        .where(Person.id == person_id, Person.email == email)  # not if Apollo has given another address since
        .values(email_check=status[:32], email_checked_at=ctx.now())
    )
    session.commit()
    return None


def _finish(ctx: JobContext, session: Session, run_id: uuid.UUID, *, note: str | None = None) -> Outcome:
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.ENRICHING or run.icp is None:
        return DONE
    icp = SiteAnalysis.model_validate(run.icp).icp
    verdicts, shortlist = _shortlist(ctx, run, icp, _candidates(session, run_id))
    if ctx.providers.verifier is not None:
        for verdict in shortlist:
            check = verdict.person.email_check
            if check in DROPPED_BY_CHECK and _fresh(ctx, verdict.person.email_checked_at):
                verdict.out = f"the email check says {check}"
    for verdict in verdicts:
        verdict.lead.icp_score = verdict.points
        verdict.lead.icp_reasons = verdict.reasons
        verdict.lead.included = verdict.out is None
        verdict.lead.exclude_reason = verdict.out[:200] if verdict.out is not None else None
    listed = sum(1 for verdict in shortlist if verdict.out is None)
    now = ctx.now()
    run.leads_count = listed
    run.status = RunStatus.READY
    run.status_detail = READY if listed else NOTHING_FOUND
    run.completed_at = now
    run.results_expire_at = now + timedelta(days=ctx.settings.results_days)
    if note is not None:
        _note(run, note)
    queue.enqueue(
        session,
        DELIVER,
        {"run_id": str(run_id), "queued_at": now.isoformat()},
        run_after=now,
        dedupe_key=f"{DELIVER}:{run_id}",
    )
    log.info("run ready", extra={"run_id": str(run_id), "leads": listed})
    return DONE


def _event_names(session: Session, run_id: uuid.UUID) -> list[str]:
    return list(
        session.scalars(
            select(Event.name)
            .join(RunEvent, RunEvent.event_id == Event.id)
            .where(RunEvent.run_id == run_id, RunEvent.status == RunEventStatus.DONE)
            .order_by(Event.start_date.asc().nulls_last(), Event.name)
        )
    )


def _profile(person: Person) -> scoring.Profile:
    return scoring.Profile(
        title=person.title or person.headline,
        seniority=person.seniority,
        industry=person.company_industry,
        employees=person.company_size,
        title_from_headline=not person.title and bool(person.headline),
    )


def _same_company(person: Person, domain: str) -> bool:
    ours = _bare(domain)
    theirs = [person.company_domain, person.email.rpartition("@")[2] if person.email else None]
    return any(
        d is not None and (d == ours or d.endswith("." + ours) or ours.endswith("." + d))
        for d in (_bare(x) if x else None for x in theirs)
    )


def _bare(domain: str) -> str:
    return domain.strip().casefold().removeprefix("www.")


def _fresh(ctx: JobContext, at: datetime | None) -> bool:
    return at is not None and at > ctx.now() - timedelta(days=ctx.settings.person_cache_days)


def _telling(lead: Lead) -> int:
    """Speakers and exhibitors first (MOST_TELLING); a label from an older version goes last."""
    try:
        return MOST_TELLING.index(AttendanceLabel(lead.attendance))
    except ValueError:
        return len(MOST_TELLING)


def _note(run: Run, text: str) -> None:
    """Add to the run's notes for the operator, keeping any earlier one (qualify notes a budget that ran out)."""
    run.error = text if not run.error else f"{run.error}; {text}"


def _cut(value: str | None, length: int) -> str | None:
    return value[:length] if value else None
