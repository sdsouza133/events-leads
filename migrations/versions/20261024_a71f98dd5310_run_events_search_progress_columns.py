"""run_events: search progress columns, and a check on status

Revision ID: a71f98dd5310
Revises: 4e39e374c269
Create Date: 2026-10-24 01:04:35.059486
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a71f98dd5310"
down_revision: str | None = "4e39e374c269"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_CHECK = "ck_run_events_run_event_status"


def upgrade() -> None:
    op.add_column(
        "run_events",
        sa.Column(
            "search_queries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="The queries the current search covers; the others came from the cache",
        ),
    )
    op.add_column("run_events", sa.Column("search_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("run_events", sa.Column("search_started_at", sa.DateTime(timezone=True), nullable=True))
    # The model stores status as an enum: a string column guarded by a check constraint, like runs.status.
    op.alter_column(
        "run_events", "status", existing_type=sa.String(length=16), type_=sa.String(length=32), existing_nullable=False
    )
    op.create_check_constraint(op.f(STATUS_CHECK), "run_events", "status IN ('pending', 'searching', 'done', 'failed')")
    op.alter_column(
        "run_events",
        "error",
        existing_type=sa.Text(),
        comment="Internal; never shown to the requester",
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "run_events",
        "error",
        existing_type=sa.Text(),
        comment=None,
        existing_comment="Internal; never shown to the requester",
        existing_nullable=True,
    )
    op.drop_constraint(op.f(STATUS_CHECK), "run_events", type_="check")
    op.alter_column(
        "run_events", "status", existing_type=sa.String(length=32), type_=sa.String(length=16), existing_nullable=False
    )
    op.drop_column("run_events", "search_started_at")
    op.drop_column("run_events", "search_attempts")
    op.drop_column("run_events", "search_queries")
