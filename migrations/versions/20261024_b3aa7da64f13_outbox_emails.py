"""outbox_emails: emails kept instead of sent where there is no Resend key (ADR 0004)

Revision ID: b3aa7da64f13
Revises: c9dec5a37310
Create Date: 2026-10-24 02:17:57.994696
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3aa7da64f13"
down_revision: str | None = "c9dec5a37310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_emails",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False, comment="The idempotency key Resend would have had"),
        sa.Column("to", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_emails")),
        sa.UniqueConstraint("key", name=op.f("uq_outbox_emails_key")),
    )
    op.create_index(op.f("ix_outbox_emails_created_at"), "outbox_emails", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_emails_created_at"), table_name="outbox_emails")
    op.drop_table("outbox_emails")
