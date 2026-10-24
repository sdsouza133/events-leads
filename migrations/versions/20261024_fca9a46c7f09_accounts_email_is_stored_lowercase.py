"""accounts.email is stored lowercase

Postgres compares text case-sensitively, so 'Jo@Acme.com' and 'jo@acme.com' would be two accounts, each with its
own runs and sign-in links. Every writer lowercases the address; this makes that hold for any future writer too.

Revision ID: fca9a46c7f09
Revises: b3aa7da64f13
Create Date: 2026-10-24 03:11:05.504202
"""

from collections.abc import Sequence

from alembic import op

revision: str = "fca9a46c7f09"
down_revision: str | None = "b3aa7da64f13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(op.f("ck_accounts_email_lowercase"), "accounts", "email = lower(email)")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_accounts_email_lowercase"), "accounts", type_="check")
