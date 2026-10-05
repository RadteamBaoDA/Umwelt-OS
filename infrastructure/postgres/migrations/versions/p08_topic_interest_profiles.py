"""Extend accepted topic rows with revisioned interest profile fields.

Revision ID: p08_topic_interest_profiles
Revises: p08_tasks_goals (provisional; root owns final ancestry)
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p08_topic_interest_profiles"
down_revision: str | Sequence[str] | None = "p08_tasks_goals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add topic profile data and lifecycle fences without recreating its table."""
    op.add_column("news_topics", sa.Column("description", sa.String(length=2000), nullable=True))
    op.add_column("news_topics", sa.Column("entity_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False))
    op.add_column("news_topics", sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False))
    op.add_column("news_topics", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_news_topics_entity_ids_array", "news_topics", "jsonb_typeof(entity_ids) = 'array' AND jsonb_array_length(entity_ids) <= 100")
    op.create_check_constraint("ck_news_topics_weight_range", "news_topics", "weight >= 0 AND weight <= 10")
    op.create_check_constraint("ck_news_topics_revision_range", "news_topics", "revision >= 1 AND revision <= 9007199254740991")
    op.create_index("ix_news_topics_owner_deleted", "news_topics", ["owner_id", "deleted_at"])


def downgrade() -> None:
    """Remove only the additive topic profile fields and their own constraints."""
    op.drop_index("ix_news_topics_owner_deleted", table_name="news_topics")
    op.drop_constraint("ck_news_topics_revision_range", "news_topics", type_="check")
    op.drop_constraint("ck_news_topics_weight_range", "news_topics", type_="check")
    op.drop_constraint("ck_news_topics_entity_ids_array", "news_topics", type_="check")
    op.drop_column("news_topics", "deleted_at")
    op.drop_column("news_topics", "revision")
    op.drop_column("news_topics", "entity_ids")
    op.drop_column("news_topics", "description")
