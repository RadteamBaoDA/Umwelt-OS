"""Add persistent chat conversations, messages, response runs, and stream events.

Revision ID: p06_chat_conversations
Revises: p05_temporal_sync
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p06_chat_conversations"
down_revision: str | Sequence[str] | None = "p05_temporal_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create chat persistence schema for conversations, messages, runs, and stream replay events."""
    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    tz_aware = sa.DateTime(timezone=True)

    # 1. chat_conversations table
    op.create_table(
        "chat_conversations",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("title", sa.String(255), nullable=False, server_default="New conversation"),
        sa.Column("context_kind", sa.String(32), nullable=True),
        sa.Column("context_resource_id", uuid_type, nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ephemeral", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", tz_aware, nullable=True),
        sa.Column("metadata", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_conversations_created_at", "chat_conversations", ["created_at"])
    op.create_index("ix_chat_conversations_updated_at", "chat_conversations", ["updated_at"])
    op.create_index("ix_chat_conversations_archived", "chat_conversations", ["archived"])

    # 2. chat_messages table
    op.create_table(
        "chat_messages",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "conversation_id",
            uuid_type,
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=True),
        sa.Column("model_identity", sa.String(128), nullable=True),
        sa.Column("citations", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("response_id", uuid_type, nullable=True),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_chat_messages_role"),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])
    op.create_index("ix_chat_messages_client_request_id", "chat_messages", ["client_request_id"])
    op.create_index("ix_chat_messages_response_id", "chat_messages", ["response_id"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])

    # 3. chat_response_runs table
    op.create_table(
        "chat_response_runs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "conversation_id",
            uuid_type,
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_message_id",
            uuid_type,
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            uuid_type,
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("client_request_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("model_alias", sa.String(64), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("token_usage", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("citations", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("retrieval_context", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ephemeral", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", tz_aware, nullable=True),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", tz_aware, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'cancelled', 'failed')",
            name="ck_chat_response_runs_status",
        ),
    )
    op.create_index("ix_chat_response_runs_conversation_id", "chat_response_runs", ["conversation_id"])
    op.create_index("ix_chat_response_runs_client_request_id", "chat_response_runs", ["client_request_id"])
    op.create_index("ix_chat_response_runs_status", "chat_response_runs", ["status"])
    op.create_index("ix_chat_response_runs_created_at", "chat_response_runs", ["created_at"])

    # 4. chat_stream_events table
    op.create_table(
        "chat_stream_events",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "response_id",
            uuid_type,
            sa.ForeignKey("chat_response_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("data", jsonb_type, nullable=False),
        sa.Column("created_at", tz_aware, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("response_id", "seq", name="uq_chat_stream_events_seq"),
        sa.UniqueConstraint("event_id", name="uq_chat_stream_events_event_id"),
        sa.CheckConstraint("seq >= 1", name="ck_chat_stream_events_seq_positive"),
    )
    op.create_index("ix_chat_stream_events_response_id", "chat_stream_events", ["response_id"])
    op.create_index("ix_chat_stream_events_seq", "chat_stream_events", ["response_id", "seq"])


def downgrade() -> None:
    """Drop chat stream events, response runs, messages, and conversations in dependency order."""
    op.drop_table("chat_stream_events")
    op.drop_table("chat_response_runs")
    op.drop_table("chat_messages")
    op.drop_table("chat_conversations")
