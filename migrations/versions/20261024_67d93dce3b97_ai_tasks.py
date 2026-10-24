"""ai_tasks: questions operators answer in manual AI mode (issue #9)

Revision ID: 67d93dce3b97
Revises: ade68021bd5a
Create Date: 2026-10-24 04:02:15.044793
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "67d93dce3b97"
down_revision: str | None = "ade68021bd5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_tasks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False, comment="sha256 of the kind and what identifies it"),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True, comment="The run that asked first"),
        sa.Column("question", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_ai_tasks_run_id_runs"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_tasks")),
        sa.UniqueConstraint("key", name=op.f("uq_ai_tasks_key")),
    )


def downgrade() -> None:
    op.drop_table("ai_tasks")
