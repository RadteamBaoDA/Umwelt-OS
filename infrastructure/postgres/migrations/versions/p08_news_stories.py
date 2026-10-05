"""Persist evidence-backed News story groups and bounded recovery progress.

Revision ID: p08_news_stories
Revises: p08_demo_seed_receipts
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p08_news_stories"
down_revision: str | Sequence[str] | None = "p08_demo_seed_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive News-owned story, observation, and catch-up tables."""
    op.create_table(
        "news_stories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("identity_key", sa.String(length=512), nullable=False),
        sa.Column("identity_kind", sa.String(length=16), nullable=False),
        sa.Column("algorithm_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_stories_identity_kind"),
        sa.CheckConstraint("algorithm_version >= 1", name="ck_news_stories_algorithm_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", "algorithm_version", name="uq_news_stories_identity_algorithm"),
    )
    op.create_index("ix_news_stories_created", "news_stories", ["created_at", "id"])
    op.create_table(
        "news_story_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.Uuid(), nullable=False),
        sa.Column("identity_key", sa.String(length=512), nullable=False),
        sa.Column("identity_kind", sa.String(length=16), nullable=False),
        sa.Column("algorithm_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_story_identities_identity_kind"),
        sa.ForeignKeyConstraint(["story_id"], ["news_stories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", "algorithm_version", name="uq_news_story_identities_key_algorithm"),
    )
    op.create_index("ix_news_story_identities_story", "news_story_identities", ["story_id"])
    op.create_table(
        "news_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("excerpt", sa.String(length=1000), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("membership_entity_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("incomplete_reason", sa.String(length=64), nullable=True),
        sa.Column("match_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("recorded_signals", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("algorithm_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_generation >= 0 AND version_number >= 1", name="ck_news_observations_generation_version"),
        sa.CheckConstraint("match_method IN ('url', 'hash', 'embedding_entity_time')", name="ck_news_observations_match_method"),
        sa.ForeignKeyConstraint(["story_id"], ["news_stories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "source_generation", "algorithm_version", name="uq_news_observations_version_generation_algorithm"),
    )
    op.create_index("ix_news_observations_story_time", "news_observations", ["story_id", "observed_at"])
    op.create_index("ix_news_observations_source_time", "news_observations", ["source_id", "observed_at"])
    op.create_table(
        "news_recovery_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_cursor", sa.String(length=512), nullable=True),
        sa.Column("document_cursor", sa.String(length=512), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_news_recovery_checkpoint_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop only unshipped News catch-up and story-owned tables."""
    op.drop_table("news_recovery_checkpoints")
    op.drop_index("ix_news_observations_source_time", table_name="news_observations")
    op.drop_index("ix_news_observations_story_time", table_name="news_observations")
    op.drop_table("news_observations")
    op.drop_index("ix_news_story_identities_story", table_name="news_story_identities")
    op.drop_table("news_story_identities")
    op.drop_index("ix_news_stories_created", table_name="news_stories")
    op.drop_table("news_stories")
