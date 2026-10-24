"""What providers hand back, independent of which service (or fake) produced it."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ProviderError(Exception):
    """A provider call failed. `retryable` tells the job queue whether trying again later can help
    (rate limits, timeouts, 5xx) or not (a rejected key, a malformed request).

    `cost_usd` is what the failed call was billed. A model can answer, and charge, and the call still fail (a
    refusal, an answer cut off, output in the wrong shape), so the caller settles its reservation with this amount
    rather than releasing it. None means unknown: the request may have been billed (a timeout mid-answer), so the
    caller settles the full reservation (ADR 0002).

    `declined` marks the one failure that is an answer about the input: the model refused this content. Other
    input may still go through, so a step working through many can set it aside. Every other failure is about the
    request or the account (a rejected key, a retired model) and says nothing about the input.
    """

    def __init__(
        self, message: str, *, retryable: bool, cost_usd: Decimal | None = Decimal(0), declined: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cost_usd = cost_usd
        self.declined = declined


class AnswerPendingError(Exception):
    """Manual AI mode: the question is waiting for an operator's answer. The worker puts the job back to ask again."""


@dataclass(frozen=True)
class Answer[T]:
    """A model's answer and what it cost, so the caller can settle the spend it reserved."""

    value: T
    cost_usd: Decimal = Decimal(0)
    model: str | None = None


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    title: str | None
    description: str | None
    text: str
    links: list[str] = field(default_factory=list)


class SearchStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class SearchRun:
    """A LinkedIn post search started with the provider (one Apify run)."""

    id: str
    status: SearchStatus
    dataset_id: str | None = None
    cost_usd: Decimal | None = None
    message: str | None = None


@dataclass(frozen=True)
class SearchedPost:
    id: str
    url: str
    text: str
    author_name: str | None
    author_url: str | None
    author_headline: str | None
    author_kind: str  # "person" or "company"
    posted_at: datetime | None
    query: str | None


@dataclass(frozen=True)
class EnrichedPerson:
    linkedin_url: str
    apollo_id: str | None
    name: str | None
    headline: str | None
    title: str | None
    seniority: str | None
    departments: list[str]
    email: str | None
    email_status: str | None
    email_domain_catchall: bool
    company_name: str | None
    company_domain: str | None
    company_industry: str | None
    company_size: int | None
    country: str | None


@dataclass(frozen=True)
class MatchResult:
    people: list[EnrichedPerson | None]  # same order as the URLs asked for; None where there was no match
    credits_used: int


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    # Required, so no email can go out without one (e.g. "results:<run id>"). Resend delivers a key once within
    # 24 hours; the outbox gives up on a send before that window ends rather than risk a second copy (ADR 0002).
    key: str
    html: str | None = None
