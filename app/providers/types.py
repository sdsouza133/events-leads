"""What providers hand back, independent of which service (or fake) produced it."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


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
    html: str | None = None
