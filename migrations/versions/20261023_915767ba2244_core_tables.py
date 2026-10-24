"""core tables: accounts, runs, caches, jobs, spend

Revision ID: 915767ba2244
Revises:
Create Date: 2026-10-23 23:32:07.166650
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "915767ba2244"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("email", name=op.f("uq_accounts_email")),
    )
    op.create_index(op.f("ix_accounts_domain"), "accounts", ["domain"], unique=False)
    op.create_table(
        "budget_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(precision=12, scale=4), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(precision=12, scale=4), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name=op.f("ck_budget_days_spend_not_negative")),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_budget_days")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False, comment="Normalized name plus year"),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("short_name", sa.String(length=100), nullable=True),
        sa.Column(
            "hashtags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("key", name=op.f("uq_events_key")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="job_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_status_run_after"), "jobs", ["status", "run_after"], unique=False)
    op.create_index(
        "uq_jobs_dedupe_key_pending",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("linkedin_url", sa.String(length=2048), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("apollo_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("seniority", sa.String(length=32), nullable=True),
        sa.Column(
            "departments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(length=300), nullable=True),
        sa.Column("company_domain", sa.String(length=253), nullable=True),
        sa.Column("company_industry", sa.String(length=200), nullable=True),
        sa.Column("company_size", sa.Integer(), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_status", sa.String(length=32), nullable=True),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_check", sa.String(length=32), nullable=True, comment="Optional second check (Reoon)"),
        sa.Column("email_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
        sa.UniqueConstraint("linkedin_url", name=op.f("uq_people_linkedin_url")),
    )
    op.create_table(
        "posts",
        sa.Column("id", sa.String(length=64), nullable=False, comment="LinkedIn activity id"),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("author_name", sa.String(length=300), nullable=True),
        sa.Column("author_url", sa.String(length=2048), nullable=True),
        sa.Column("author_headline", sa.Text(), nullable=True),
        sa.Column("author_kind", sa.String(length=16), nullable=False, comment="person or company"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posts")),
    )
    op.create_index(op.f("ix_posts_author_url"), "posts", ["author_url"], unique=False)
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limits")),
    )
    op.create_table(
        "event_posts",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.String(length=64), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("found_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_posts_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name=op.f("fk_event_posts_post_id_posts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", "post_id", name=op.f("pk_event_posts")),
    )
    op.create_table(
        "post_labels",
        sa.Column("post_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False, comment="rule or model"),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_post_labels_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name=op.f("fk_post_labels_post_id_posts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("post_id", "event_id", name=op.f("pk_post_labels")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("website_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "awaiting_sign_in",
                "analyzing",
                "awaiting_approval",
                "collecting",
                "waiting_for_budget",
                "qualifying",
                "enriching",
                "ready",
                "failed",
                "canceled",
                "expired",
                name="run_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("status_detail", sa.Text(), nullable=True, comment="Shown to the requester"),
        sa.Column("error", sa.Text(), nullable=True, comment="Internal; never shown to the requester"),
        sa.Column("icp", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("suggested_events", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("budget_usd", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("spent_usd", sa.Numeric(precision=12, scale=4), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(precision=12, scale=4), server_default=sa.text("0"), nullable=False),
        sa.Column("apollo_credits_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("leads_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("results_expire_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name=op.f("ck_runs_spend_not_negative")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_runs_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(op.f("ix_runs_account_id"), "runs", ["account_id"], unique=False)
    op.create_index(op.f("ix_runs_domain_created_at"), "runs", ["domain", "created_at"], unique=False)
    op.create_index(op.f("ix_runs_status"), "runs", ["status"], unique=False)
    op.create_table(
        "search_cache",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("post_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=4), server_default=sa.text("0"), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_search_cache_event_id_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_cache")),
        sa.UniqueConstraint("event_id", "query", name=op.f("uq_search_cache_event_id_query")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_sessions_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_account_id"), "sessions", ["account_id"], unique=False)
    op.create_table(
        "login_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_login_tokens_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_login_tokens_run_id_runs"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_login_tokens_token_hash")),
    )
    op.create_index(op.f("ix_login_tokens_account_id"), "login_tokens", ["account_id"], unique=False)
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, comment="suggested or requester"),
        sa.Column(
            "queries", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("apify_run_id", sa.String(length=64), nullable=True),
        sa.Column("spend_id", sa.BigInteger(), nullable=True, comment="Reservation for the current search"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_run_events_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "event_id", name=op.f("pk_run_events")),
    )
    op.create_table(
        "run_leads",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.String(length=64), nullable=False),
        sa.Column("attendance", sa.String(length=32), nullable=False),
        sa.Column("icp_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "icp_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("included", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("exclude_reason", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_run_leads_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["people.id"], name=op.f("fk_run_leads_person_id_people"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], name=op.f("fk_run_leads_post_id_posts"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_leads_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "person_id", name=op.f("pk_run_leads")),
    )
    op.create_table(
        "spend",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("day", sa.Date(), nullable=False, comment="The budget day the reservation counted against"),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "reserved",
                "settled",
                "released",
                name="spend_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("estimated_usd", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("actual_usd", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("units", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_spend_run_id_runs"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend")),
    )
    op.create_index(op.f("ix_spend_day"), "spend", ["day"], unique=False)
    op.create_index(op.f("ix_spend_run_id"), "spend", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_spend_run_id"), table_name="spend")
    op.drop_index(op.f("ix_spend_day"), table_name="spend")
    op.drop_table("spend")
    op.drop_table("run_leads")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_login_tokens_account_id"), table_name="login_tokens")
    op.drop_table("login_tokens")
    op.drop_index(op.f("ix_sessions_account_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("search_cache")
    op.drop_index(op.f("ix_runs_status"), table_name="runs")
    op.drop_index(op.f("ix_runs_domain_created_at"), table_name="runs")
    op.drop_index(op.f("ix_runs_account_id"), table_name="runs")
    op.drop_table("runs")
    op.drop_table("post_labels")
    op.drop_table("event_posts")
    op.drop_table("rate_limits")
    op.drop_index(op.f("ix_posts_author_url"), table_name="posts")
    op.drop_table("posts")
    op.drop_table("people")
    op.drop_index(
        "uq_jobs_dedupe_key_pending", table_name="jobs", postgresql_where=sa.text("status IN ('queued', 'running')")
    )
    op.drop_index(op.f("ix_jobs_status_run_after"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("events")
    op.drop_table("budget_days")
    op.drop_index(op.f("ix_accounts_domain"), table_name="accounts")
    op.drop_table("accounts")
