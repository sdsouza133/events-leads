"""operator runs and flags: runs an operator starts from the console, and the console's switches

Revision ID: d3019701d7fa
Revises: e9544e134033
Create Date: 2026-10-24 19:31:57.139160
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3019701d7fa"
down_revision: str | None = "e9544e134033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "flags",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["accounts.id"], name=op.f("fk_flags_changed_by_accounts"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_flags")),
    )
    op.add_column(
        "runs",
        sa.Column(
            "operator_run",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Started from the console by an operator, so it doesn't use the company's free run",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "operator_run")
    op.drop_table("flags")
