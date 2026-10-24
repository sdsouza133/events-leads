"""retention: a lead keeps its person and post; indexes for deleting old data (issue #18)

Deleting a person or a post used to delete every lead that pointed at it. Now the database refuses (RESTRICT), so
deleting old shared data can never take someone off a list that is still shown. The indexes let the retention job
find old posts and labels, and let Postgres check a person or post has no lead left before deleting it.

Revision ID: e9544e134033
Revises: 67d93dce3b97
Create Date: 2026-10-24 04:36:27.909744
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e9544e134033"
down_revision: str | None = "67d93dce3b97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_post_labels_created_at"), "post_labels", ["created_at"], unique=False)
    op.create_index(op.f("ix_posts_fetched_at"), "posts", ["fetched_at"], unique=False)
    op.create_index(op.f("ix_run_leads_person_id"), "run_leads", ["person_id"], unique=False)
    op.create_index(op.f("ix_run_leads_post_id"), "run_leads", ["post_id"], unique=False)
    op.drop_constraint(op.f("fk_run_leads_person_id_people"), "run_leads", type_="foreignkey")
    op.drop_constraint(op.f("fk_run_leads_post_id_posts"), "run_leads", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_run_leads_person_id_people"), "run_leads", "people", ["person_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        op.f("fk_run_leads_post_id_posts"), "run_leads", "posts", ["post_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_run_leads_post_id_posts"), "run_leads", type_="foreignkey")
    op.drop_constraint(op.f("fk_run_leads_person_id_people"), "run_leads", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_run_leads_post_id_posts"), "run_leads", "posts", ["post_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        op.f("fk_run_leads_person_id_people"), "run_leads", "people", ["person_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_index(op.f("ix_run_leads_post_id"), table_name="run_leads")
    op.drop_index(op.f("ix_run_leads_person_id"), table_name="run_leads")
    op.drop_index(op.f("ix_posts_fetched_at"), table_name="posts")
    op.drop_index(op.f("ix_post_labels_created_at"), table_name="post_labels")
