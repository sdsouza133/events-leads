"""Qualify: who really went (or is going) to the run's events, and who could fit the ICP (issue #6).

Every post an event's searches found is labeled once, and the label is kept for that post and event, so other runs
that search the same event reuse it instead of paying again:

1. Text rules label the obvious posts for free (`app/pipeline/rules.py`).
2. Claude labels the rest, up to 25 posts per call. Each call is a paid call (ADR 0002) whose labels are stored with
   its cost. After a few calls the job defers to itself, so other runs' jobs get a turn and a crash costs one call.

Company pages are never labeled. The posts that show their author there (attending, attended, speaking,
exhibiting) give the run's candidates, one per person, each with their most telling post; organizers, promoters,
people not going and unclear posts are left out. A candidate whose headline already rules them out (a student, a job
title the ICP excludes, the requester's own colleague) is kept with the reason, before any paid lookup. Then the run
moves on to enrich.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import budget, queue
from app.budget import BudgetExceededError
from app.models import Event, EventPost, Lead, Person, Post, PostLabel, Run, RunEvent, RunEventStatus, RunStatus
from app.pipeline import rules, runs
from app.pipeline.base import DONE, Defer, JobContext, Outcome, Payload
from app.pipeline.paid import paid_call, step_key
from app.providers.pricing import claude_label_estimate
from app.providers.types import ProviderError
from app.schemas import QUALIFYING_LABELS, AttendanceLabel, LabeledPost, PostToLabel, SiteAnalysis

QUALIFY = "qualify"
ENRICH = "enrich"  # the next step's job (issue #7)
BATCH_SIZE = 25
CALLS_PER_TURN = 4
NEXT_TURN = timedelta(0)  # back of the queue, at once

ENRICHING = "Finding work emails for the people who were there"
WAITING = "We've reached today's capacity. We'll finish reading the posts automatically after midnight UTC."
SOMETHING_WRONG = "Something went wrong while reading the posts. We've been notified."

# When several posts show the same person at the run's events, the most telling one is kept.
_PREFERENCE = [
    AttendanceLabel.SPEAKING,
    AttendanceLabel.EXHIBITING,
    AttendanceLabel.ATTENDING,
    AttendanceLabel.ATTENDED,
]


class _Stop(Enum):
    LABELING = "the run's budget is used up: go on with the posts already labeled"


def qualify(ctx: JobContext, session: Session, payload: Payload) -> Outcome:
    run_id = uuid.UUID(payload["run_id"])
    run = session.get(Run, run_id)
    if run is None or run.status != RunStatus.QUALIFYING:
        return DONE
    events = [
        (event_id, name)
        for event_id, name in session.execute(
            select(Event.id, Event.name)
            .join(RunEvent, RunEvent.event_id == Event.id)
            .where(RunEvent.run_id == run_id, RunEvent.status == RunEventStatus.DONE)
            .order_by(Event.id)
        )
    ]
    session.commit()

    calls = 0
    for event_id, event_name in events:
        _label_by_rules(session, event_id)
        todo = _unlabeled(session, event_id)
        for start in range(0, len(todo), BATCH_SIZE):
            if calls == CALLS_PER_TURN:
                return Defer(NEXT_TURN)
            batch = todo[start : start + BATCH_SIZE]
            outcome = _label_by_claude(ctx, session, run_id, event_id, event_name=event_name, batch=batch)
            if outcome is _Stop.LABELING:
                return _choose_candidates(ctx, session, run_id, note=outcome.value)
            if outcome is not None:
                return outcome
            calls += 1
    return _choose_candidates(ctx, session, run_id)


def give_up(ctx: JobContext, session: Session, payload: Payload) -> None:
    """The job failed for good: close any labeling call it left open, and fail the run."""
    run_id = uuid.UUID(payload["run_id"])
    event_ids = session.scalars(select(RunEvent.event_id).where(RunEvent.run_id == run_id)).all()
    for event_id in event_ids:
        budget.recover(session, step_key(run_id, _step(event_id)), now=ctx.now())
    run = runs.locked(session, run_id)
    if run is not None and run.status == RunStatus.QUALIFYING:
        runs.fail(run, SOMETHING_WRONG, now=ctx.now(), error="qualify job failed after its last attempt")


def _label_by_rules(session: Session, event_id: uuid.UUID) -> None:
    found = [
        LabeledPost(post_id=post.post_id, label=name, confidence=rules.RULE_CONFIDENCE, reason="matched a text rule")
        for post in _unlabeled(session, event_id)
        if (name := rules.label(post.text)) is not None
    ]
    _store(session, event_id, found, method="rule")
    session.commit()


def _label_by_claude(
    ctx: JobContext,
    session: Session,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    event_name: str,
    batch: list[PostToLabel],
) -> Outcome | _Stop | None:
    """Label one batch. None when it was labeled; otherwise what the job should do instead of going on."""
    model = ctx.settings.classify_model
    try:
        paid = paid_call(
            ctx,
            run_id=run_id,
            step=_step(event_id),
            provider="claude",
            estimate=claude_label_estimate(model, len(batch)),
            call=lambda: ctx.providers.llm.label_posts(event_name, batch),
        )
    except BudgetExceededError as exc:
        if exc.scope == "run":
            return _Stop.LABELING
        run = runs.locked(session, run_id)
        if run is not None and run.status == RunStatus.QUALIFYING:
            run.status_detail = WAITING
        return Defer(runs.until_tomorrow(ctx.now()))
    except ProviderError as exc:
        if not exc.declined:
            # Retried when that can help (a timeout). Otherwise (a rejected key, a retired model) the job fails, and
            # the run with it, without caching anything: the labels are shared, and these posts deserve a real one.
            raise
        # Claude refused these posts' content: keep them as unclear, so no run asks again.
        reason = f"not labeled: {exc}"[:300]
        unclear = [
            LabeledPost(post_id=p.post_id, label=AttendanceLabel.UNCLEAR, confidence=0, reason=reason) for p in batch
        ]
        _store(session, event_id, unclear, method="skipped")
        session.commit()
        return None

    budget.settle(session, paid.spend_id, paid.answer.cost_usd, units=Decimal(len(batch)), unit="post", now=ctx.now())
    _store(session, event_id, paid.answer.value, method="model", model=paid.answer.model)
    session.commit()  # the labels and their cost, together
    return None


def _choose_candidates(ctx: JobContext, session: Session, run_id: uuid.UUID, *, note: str | None = None) -> Outcome:
    run = runs.locked(session, run_id)
    if run is None or run.status != RunStatus.QUALIFYING or run.icp is None:
        return DONE
    analysis = SiteAnalysis.model_validate(run.icp)
    for url, seen in sorted(_best_post_per_person(session, run_id).items()):
        session.execute(
            insert(Lead)
            .values(
                run_id=run_id,
                person_id=_person_id(session, url, seen.author_name, seen.headline),
                event_id=seen.event_id,
                post_id=seen.post_id,
                attendance=str(seen.attendance),
                exclude_reason=rules.prescreen(seen.headline, analysis.icp, own_company=analysis.company_name),
            )
            .on_conflict_do_nothing()
        )
    run.status = RunStatus.ENRICHING
    run.status_detail = ENRICHING
    if note is not None:
        run.error = note
    queue.enqueue(session, ENRICH, {"run_id": str(run_id)}, run_after=ctx.now(), dedupe_key=f"{ENRICH}:{run_id}")
    return DONE


@dataclass(frozen=True)
class _Seen:
    """A post that shows its author at one of the run's events."""

    post_id: str
    author_url: str
    author_name: str | None
    headline: str | None
    posted_at: datetime | None
    attendance: AttendanceLabel
    event_id: uuid.UUID

    def rank(self) -> tuple[int, float]:
        """Lower is better: the most telling label, then the most recent post."""
        posted = self.posted_at.timestamp() if self.posted_at is not None else 0.0
        return _PREFERENCE.index(self.attendance), -posted


def _best_post_per_person(session: Session, run_id: uuid.UUID) -> dict[str, _Seen]:
    """Each person's most telling post among those that show them at one of the run's events."""
    rows = session.execute(
        select(
            Post.id,
            Post.author_url,
            Post.author_name,
            Post.author_headline,
            Post.posted_at,
            PostLabel.label,
            PostLabel.event_id,
        )
        .join(PostLabel, PostLabel.post_id == Post.id)
        .join(
            RunEvent,
            and_(
                RunEvent.event_id == PostLabel.event_id,
                RunEvent.run_id == run_id,
                RunEvent.status == RunEventStatus.DONE,
            ),
        )
        .where(
            PostLabel.label.in_([str(name) for name in QUALIFYING_LABELS]),
            Post.author_kind == "person",
            Post.author_url.is_not(None),
        )
        .order_by(Post.id)
    )
    best: dict[str, _Seen] = {}
    for post_id, url, name, headline, posted_at, label, event_id in rows:
        if url is None:  # (the query leaves these out; this tells the type checker)
            continue
        seen = _Seen(post_id, url, name, headline, posted_at, AttendanceLabel(label), event_id)
        kept = best.get(url)
        if kept is None or seen.rank() < kept.rank():
            best[url] = seen
    return best


def _unlabeled(session: Session, event_id: uuid.UUID) -> list[PostToLabel]:
    """The event's posts by people (company pages aren't worth labeling) that have no label yet."""
    labeled = select(PostLabel.post_id).where(PostLabel.event_id == event_id)
    rows = session.execute(
        select(Post.id, Post.author_headline, Post.text)
        .join(EventPost, EventPost.post_id == Post.id)
        .where(
            EventPost.event_id == event_id,
            Post.author_kind == "person",
            Post.author_url.is_not(None),
            Post.id.not_in(labeled),
        )
        .order_by(Post.id)
    )
    return [PostToLabel(post_id=post_id, author_headline=headline, text=text) for post_id, headline, text in rows]


def _person_id(session: Session, url: str, name: str | None, headline: str | None) -> uuid.UUID:
    """The shared person row for this profile, created the first time any run finds them."""
    session.execute(
        insert(Person)
        .values(id=uuid.uuid4(), linkedin_url=url, name=name, headline=headline)
        .on_conflict_do_nothing(index_elements=[Person.linkedin_url])
    )
    return session.scalars(select(Person.id).where(Person.linkedin_url == url)).one()


def _store(
    session: Session, event_id: uuid.UUID, labels: list[LabeledPost], *, method: str, model: str | None = None
) -> None:
    """Cache the labels for the event. The first label a post gets for an event is the one every run uses."""
    if not labels:
        return
    rows = [
        {
            "post_id": labeled.post_id,
            "event_id": event_id,
            "label": str(labeled.label),
            "confidence": labeled.confidence,
            "reason": labeled.reason,
            "method": method,
            "model": model,
        }
        for labeled in labels
    ]
    session.execute(insert(PostLabel).values(rows).on_conflict_do_nothing())


def _step(event_id: uuid.UUID) -> str:
    return f"qualify:{event_id}"
