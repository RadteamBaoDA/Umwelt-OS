"""Add selective memory items, candidates, and memory privacy controls.

Revision ID: p06_selective_memory
Revises: p06_chat_conversations
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p06_selective_memory"
down_revision: str | Sequence[str] | None = "p06_chat_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create memory candidates, persistent memories, and privacy settings tables."""
    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    tz_aware = sa.DateTime(timezone=True)

    # 1. memory_candidates table
    op.create_table(
        "memory_candidates",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False, server_default="fact"),
        sa.Column("provenance", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("novelty_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("usefulness_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("evaluated_at", tz_aware, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'superseded', 'expired')",
            name="ck_memory_candidates_status",
        ),
        sa.CheckConstraint(
            "memory_type IN ('fact', 'preference', 'instruction', 'decision', 'procedural')",
            name="ck_memory_candidates_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_candidates_confidence_range",
        ),
        sa.CheckConstraint(
            "novelty_score >= 0.0 AND novelty_score <= 1.0",
            name="ck_memory_candidates_novelty_range",
        ),
        sa.CheckConstraint(
            "usefulness_score >= 0.0 AND usefulness_score <= 1.0",
            name="ck_memory_candidates_usefulness_range",
        ),
    )
    op.create_index("ix_memory_candidates_status", "memory_candidates", ["status"])
    op.create_index("ix_memory_candidates_created_at", "memory_candidates", ["created_at"])

    # 2. memories table
    op.create_table(
        "memories",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False, server_default="fact"),
        sa.Column("provenance", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "superseded_by_id",
            uuid_type,
            sa.ForeignKey("memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "candidate_id",
            uuid_type,
            sa.ForeignKey("memory_candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("invalidated_at", tz_aware, nullable=True),
        sa.Column("forgotten_at", tz_aware, nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'invalidated', 'superseded', 'forgotten')",
            name="ck_memories_status",
        ),
        sa.CheckConstraint(
            "memory_type IN ('fact', 'preference', 'instruction', 'decision', 'procedural')",
            name="ck_memories_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memories_confidence_range",
        ),
    )
    op.create_index("ix_memories_status", "memories", ["status"])
    op.create_index("ix_memories_memory_type", "memories", ["memory_type"])
    op.create_index("ix_memories_created_at", "memories", ["created_at"])
    op.create_index("ix_memories_is_manual", "memories", ["is_manual"])

    # 3. memory_privacy_settings table
    op.create_table(
        "memory_privacy_settings",
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("owner.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "store_conversation_history",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "store_agent_memory",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "auto_accept_memory",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_id = 1", name="ck_memory_privacy_settings_single_owner"),
    )


def downgrade() -> None:
    """Drop memory privacy settings, memories, and memory candidates in reverse dependency order."""
    op.drop_table("memory_privacy_settings")
    op.drop_table("memories")
    op.drop_table("memory_candidates")
