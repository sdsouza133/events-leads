"""Database tables. Every change here needs an Alembic migration: `make migration m="what changed"`."""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit names for constraints and indexes, so migrations are reproducible and reviewable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(12, 4)
"""US dollars to 1/100 of a cent: Apify charges $0.002 per post."""


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        Decimal: Money,
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """Store enums as strings guarded by a check constraint: adding a value later is a one-line constraint change,
    not a Postgres ALTER TYPE."""
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
    )


class RunStatus(enum.StrEnum):
    AWAITING_SIGN_IN = "awaiting_sign_in"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    COLLECTING = "collecting"
    WAITING_FOR_BUDGET = "waiting_for_budget"
    QUALIFYING = "qualifying"
    ENRICHING = "enriching"
    READY = "ready"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SpendStatus(enum.StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"


class RunEventStatus(enum.StrEnum):
    PENDING = "pending"  # not searched yet, or waiting to search again
    SEARCHING = "searching"
    DONE = "done"
    FAILED = "failed"  # couldn't be searched; the run goes on without it


# ----------------------------------------------------------------------------------------------- people who use it
class Account(Base):
    """A requester or operator, identified by a work email."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    domain: Mapped[str] = mapped_column(String(253), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    verified_at: Mapped[datetime | None]
    last_sign_in_at: Mapped[datetime | None]


class LoginToken(Base):
    """A one-time sign-in link. Only the SHA-256 of the token is stored."""

    __tablename__ = "login_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class UserSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored, so sessions are revocable."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


# ---------------------------------------------------------------------------------------------------------- runs
class Run(Base):
    """One company's request, from sign-in to results."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name="spend_not_negative"),
        Index(None, "domain", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(253))
    website_url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), index=True, default=RunStatus.AWAITING_SIGN_IN
    )
    status_detail: Mapped[str | None] = mapped_column(Text, comment="Shown to the requester")
    error: Mapped[str | None] = mapped_column(Text, comment="Internal; never shown to the requester")
    icp: Mapped[dict[str, Any] | None]
    suggested_events: Mapped[list[Any] | None]
    budget_usd: Mapped[Decimal]
    spent_usd: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))
    reserved_usd: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))
    apollo_credits_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    leads_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    approved_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    results_expire_at: Mapped[datetime | None]


class Event(Base):
    """A real-world event, shared across runs so its posts are fetched once."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(200), unique=True, comment="Normalized name plus year")
    name: Mapped[str] = mapped_column(String(300))
    short_name: Mapped[str | None] = mapped_column(String(120))  # as long as the schemas allow (ShortText)
    hashtags: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RunEvent(Base):
    """An event a run searches, with its approved queries and collection progress."""

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), comment="suggested or requester")
    queries: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    status: Mapped[RunEventStatus] = mapped_column(
        _enum(RunEventStatus, "run_event_status"), default=RunEventStatus.PENDING
    )
    apify_run_id: Mapped[str | None] = mapped_column(String(64))
    spend_id: Mapped[int | None] = mapped_column(BigInteger, comment="Reservation for the current search")
    search_queries: Mapped[list[Any]] = mapped_column(
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="The queries the current search covers; the others came from the cache",
    )
    search_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    search_started_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text, comment="Internal; never shown to the requester")
    collected_at: Mapped[datetime | None]


# ----------------------------------------------------------------------------------- shared caches (no run owner)
class Post(Base):
    """A public LinkedIn post."""

    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="LinkedIn activity id")
    url: Mapped[str] = mapped_column(String(2048))
    author_name: Mapped[str | None] = mapped_column(String(300))
    author_url: Mapped[str | None] = mapped_column(String(2048), index=True)
    author_headline: Mapped[str | None] = mapped_column(Text)
    author_kind: Mapped[str] = mapped_column(String(16), default="person", comment="person or company")
    text: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EventPost(Base):
    __tablename__ = "event_posts"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True)
    query: Mapped[str] = mapped_column(String(500))
    found_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SearchCache(Base):
    """Which searches already ran, so a popular event isn't searched again within the cache window."""

    __tablename__ = "search_cache"
    __table_args__ = (UniqueConstraint("event_id", "query"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String(500))
    post_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PostLabel(Base):
    """Whether a post shows its author attending an event. Reused by every run that searches that event."""

    __tablename__ = "post_labels"

    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    label: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    reason: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(16), comment="rule or model")
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Person(Base):
    """One LinkedIn profile with what Apollo knows about it. Shared across runs to avoid paying twice."""

    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    linkedin_url: Mapped[str] = mapped_column(String(2048), unique=True)
    name: Mapped[str | None] = mapped_column(String(300))
    headline: Mapped[str | None] = mapped_column(Text)
    apollo_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(300))
    seniority: Mapped[str | None] = mapped_column(String(32))
    departments: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    company_name: Mapped[str | None] = mapped_column(String(300))
    company_domain: Mapped[str | None] = mapped_column(String(253))
    company_industry: Mapped[str | None] = mapped_column(String(200))
    company_size: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    email_status: Mapped[str | None] = mapped_column(String(32))
    enriched_at: Mapped[datetime | None]
    email_check: Mapped[str | None] = mapped_column(String(32), comment="Optional second check (Reoon)")
    email_checked_at: Mapped[datetime | None]


class Lead(Base):
    """A person on a run's list, with why they were included or left out."""

    __tablename__ = "run_leads"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    attendance: Mapped[str] = mapped_column(String(32))
    icp_score: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    icp_reasons: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    included: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    exclude_reason: Mapped[str | None] = mapped_column(String(200))


# ------------------------------------------------------------------------------------------- operations and money
class Job(Base):
    """A unit of background work. See app/queue.py and ADR 0002."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index(None, "status", "run_after"),
        # At most one pending copy of a job with the same dedupe key.
        Index(
            "uq_jobs_dedupe_key_pending",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.QUEUED, server_default=JobStatus.QUEUED.value
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SpendEntry(Base):
    """One paid call: reserved before it runs, settled with the actual cost (or released) after."""

    __tablename__ = "spend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True, comment="The budget day the reservation counted against")
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[SpendStatus] = mapped_column(_enum(SpendStatus, "spend_status"), default=SpendStatus.RESERVED)
    estimated_usd: Mapped[Decimal]
    actual_usd: Mapped[Decimal | None]
    units: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(String(300))
    step_key: Mapped[str | None] = mapped_column(
        String(200), index=True, comment="The paid step this is for, e.g. '<run id>:analyze_site' (ADR 0002 recovery)"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(comment="When the call began; unset means it never started")
    settled_at: Mapped[datetime | None]


class BudgetDay(Base):
    """Running totals for one UTC day, locked while reserving so concurrent workers can't pass the daily cap."""

    __tablename__ = "budget_days"
    __table_args__ = (CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name="spend_not_negative"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_usd: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))
    reserved_usd: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))


class RateLimit(Base):
    """Fixed-window counters, e.g. sign-up attempts per IP per hour."""

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class OutboxEmail(Base):
    """An email kept instead of sent, where there's no Resend key: local work and previews (ADR 0004). Production
    requires the key, so nothing is ever written here there."""

    __tablename__ = "outbox_emails"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True, comment="The idempotency key Resend would have had")
    to: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
