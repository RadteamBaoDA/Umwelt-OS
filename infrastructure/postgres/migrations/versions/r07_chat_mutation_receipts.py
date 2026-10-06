"""Add durable idempotency receipts for append-only chat prompt edits and regenerations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r07_chat_mutation_receipts"
down_revision: str | Sequence[str] | None = "r14_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create conversation-scoped receipts so retries cannot append a second chat mutation."""
    op.add_column(
        "chat_messages",
        sa.Column("revision_of_message_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_chat_messages_revision_of_message_id",
        "chat_messages",
        "chat_messages",
        ["revision_of_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_chat_messages_revision_of_message_id",
        "chat_messages",
        ["revision_of_message_id"],
    )
    op.create_table(
        "chat_message_mutation_receipts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("target_message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("result_user_message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("response_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('edit', 'regenerate')", name="ck_chat_message_mutation_action"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_chat_message_mutation_digest"),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["result_user_message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["response_id"], ["chat_response_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "client_request_id", name="uq_chat_message_mutation_request"),
    )
    op.create_index(
        "ix_chat_message_mutation_receipts_response_id",
        "chat_message_mutation_receipts",
        ["response_id"],
    )


def downgrade() -> None:
    """Remove only the additive chat mutation receipt table."""
    op.drop_index(
        "ix_chat_message_mutation_receipts_response_id",
        table_name="chat_message_mutation_receipts",
    )
    op.drop_table("chat_message_mutation_receipts")
    op.drop_index("ix_chat_messages_revision_of_message_id", table_name="chat_messages")
    op.drop_constraint("fk_chat_messages_revision_of_message_id", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "revision_of_message_id")
