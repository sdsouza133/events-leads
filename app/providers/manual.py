"""Manual AI mode (issue #9): while no Claude API key is set, each question for Claude becomes a task that an
operator answers through the operator API (`app/web/operator_api.py`). The job that asked waits and asks again, and
once the answer is in it carries on as if Claude had answered, at no cost.

A task is found again by what identifies its question (the website, the analysis, the posts), not by everything in
it, so a page that reads a little differently the next time still finds its task. Answers are checked against the
schemas Claude's answers are held to, before they're stored.
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import session_scope
from app.models import AiTask
from app.providers.anthropic_llm import EVENTS_LOOK_AHEAD, EVENTS_LOOK_BACK, MAX_EVENTS
from app.providers.types import Answer, AnswerPendingError, FetchedPage
from app.schemas import AttendanceLabel, EventSuggestion, LabeledPost, PostToLabel, SiteAnalysis

ANALYZE_SITE = "analyze_site"
FIND_EVENTS = "find_events"
LABEL_POSTS = "label_posts"
MODEL = "manual"
FREE = Decimal(0)

ASKING_RUN: ContextVar[uuid.UUID | None] = ContextVar("asking_run", default=None)
"""The run whose job is asking, so a task can say which run waits on it. The worker sets it (`asking_for`)."""


class EventsAnswer(BaseModel):
    """What find_events asks for: up to 8 events, each with the page it was found on."""

    model_config = ConfigDict(extra="forbid")

    events: list[EventSuggestion] = Field(max_length=MAX_EVENTS)


class LabelsAnswer(BaseModel):
    """What label_posts asks for: one label for each post."""

    model_config = ConfigDict(extra="forbid")

    labels: list[LabeledPost]


SHAPES: dict[str, type[BaseModel]] = {ANALYZE_SITE: SiteAnalysis, FIND_EVENTS: EventsAnswer, LABEL_POSTS: LabelsAnswer}
"""The schema each kind of answer must fit."""


class ManualLlm:
    """Claude's stand-in: each question waits as a task until an operator answers it."""

    def analyze_site(self, pages: list[FetchedPage]) -> Answer[SiteAnalysis]:
        if not pages:
            raise ValueError("no pages to analyze")
        question = {
            "pages": [{"url": p.url, "title": p.title, "description": p.description, "text": p.text} for p in pages]
        }
        answer = _ask(ANALYZE_SITE, identity=pages[0].url, question=question)
        return Answer(SiteAnalysis.model_validate(answer), FREE, MODEL)

    def find_events(self, analysis: SiteAnalysis, *, today: date) -> Answer[list[EventSuggestion]]:
        about = analysis.model_dump(mode="json")
        answer = _ask(FIND_EVENTS, identity=about, question={"analysis": about, "today": today.isoformat()})
        return Answer(EventsAnswer.model_validate(answer).events, FREE, MODEL)

    def label_posts(self, event_name: str, posts: list[PostToLabel]) -> Answer[list[LabeledPost]]:
        if not posts:
            return Answer([], FREE, MODEL)
        identity = {"event": event_name, "posts": sorted(post.post_id for post in posts)}
        question = {"event": event_name, "posts": [post.model_dump(mode="json") for post in posts]}
        given = LabelsAnswer.model_validate(_ask(LABEL_POSTS, identity=identity, question=question)).labels
        labels = {label.post_id: label for label in given}
        return Answer(
            [
                labels.get(p.post_id)
                or LabeledPost(
                    post_id=p.post_id, label=AttendanceLabel.UNCLEAR, confidence=0.0, reason="no label given"
                )
                for p in posts
            ],
            FREE,
            MODEL,
        )


def problems(question: dict[str, Any], answer: BaseModel) -> list[str]:
    """What, beyond its shape, makes an answer wrong for its question. Empty when it's right."""
    found: list[str] = []
    if isinstance(answer, EventsAnswer):
        today = date.fromisoformat(question["today"])
        earliest, latest = today - EVENTS_LOOK_BACK, today + EVENTS_LOOK_AHEAD
        names: set[str] = set()
        for i, event in enumerate(answer.events):
            ends, starts = event.end_date or event.start_date, event.start_date or event.end_date
            if (ends and ends < earliest) or (starts and starts > latest):
                found.append(f"events.{i}: not between {earliest} and {latest}")
            name = " ".join(event.name.lower().split())
            if name in names:
                found.append(f"events.{i}: the same event twice")
            names.add(name)
    elif isinstance(answer, LabelsAnswer):
        asked = sorted(post["post_id"] for post in question["posts"])
        if sorted(label.post_id for label in answer.labels) != asked:
            found.append("labels: give exactly one label for each post asked about")
    return found


@contextmanager
def asking_for(run_id: object) -> Iterator[None]:
    """While a job runs: the run any task it opens is for (its payload's run_id, when it has one)."""
    try:
        run = uuid.UUID(str(run_id)) if run_id is not None else None
    except ValueError:
        run = None
    token = ASKING_RUN.set(run)
    try:
        yield
    finally:
        ASKING_RUN.reset(token)


def _ask(kind: str, *, identity: object, question: dict[str, Any]) -> dict[str, Any]:
    """The operator's answer, once given. Until then the question waits as a task, and AnswerPendingError says so."""
    key = hashlib.sha256(json.dumps([kind, identity], sort_keys=True).encode()).hexdigest()
    with session_scope() as session:
        session.execute(
            insert(AiTask)
            .values(key=key, kind=kind, run_id=ASKING_RUN.get(), question=question)
            .on_conflict_do_nothing(index_elements=[AiTask.key])
        )
        answer: dict[str, Any] | None = session.scalar(select(AiTask.answer).where(AiTask.key == key))
    if answer is None:
        raise AnswerPendingError(f"{kind} is waiting for an operator's answer")
    return answer
