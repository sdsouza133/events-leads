"""run_grants: extra runs an operator gives a company (issue #9)

Revision ID: ade68021bd5a
Revises: fca9a46c7f09
Create Date: 2026-10-24 03:42:48.362059
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ade68021bd5a"
down_revision: str | None = "fca9a46c7f09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_grants",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["accounts.id"], name=op.f("fk_run_grants_granted_by_accounts"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_grants")),
    )
    op.create_index(op.f("ix_run_grants_domain"), "run_grants", ["domain"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_run_grants_domain"), table_name="run_grants")
    op.drop_table("run_grants")
