"""spend: started_at and step_key for crash recovery

Revision ID: 4e39e374c269
Revises: 915767ba2244
Create Date: 2026-10-24 00:34:49.044196
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e39e374c269"
down_revision: str | None = "915767ba2244"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spend",
        sa.Column(
            "step_key",
            sa.String(length=200),
            nullable=True,
            comment="The paid step this is for, e.g. '<run id>:analyze_site' (ADR 0002 recovery)",
        ),
    )
    op.add_column(
        "spend",
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the call began; unset means it never started",
        ),
    )
    op.create_index(op.f("ix_spend_step_key"), "spend", ["step_key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_spend_step_key"), table_name="spend")
    op.drop_column("spend", "started_at")
    op.drop_column("spend", "step_key")
