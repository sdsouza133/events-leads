"""events.short_name: 120 characters, as many as the schemas accept

A suggested or edited short name of 101 to 120 characters passed validation and then failed the insert, so the
requester couldn't approve their plan.

Revision ID: c9dec5a37310
Revises: a71f98dd5310
Create Date: 2026-10-24 02:11:49.308924
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9dec5a37310"
down_revision: str | None = "a71f98dd5310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "events", "short_name", existing_type=sa.String(length=100), type_=sa.String(length=120), existing_nullable=True
    )


def downgrade() -> None:
    op.alter_column(
        "events",
        "short_name",
        existing_type=sa.String(length=120),
        type_=sa.String(length=100),
        existing_nullable=True,
        postgresql_using="left(short_name, 100)",
    )
