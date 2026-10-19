"""Add conversation-scoped chat todos.

Rows cascade with their conversation (composite workspace/conversation FK, like chat_response_runs), so
deletion and retention purges need no extra cleanup. Todos hold no document or evidence content.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r22_chat_todos"
down_revision: str | Sequence[str] | None = "r21_drop_backup_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create chat_todos with its status/position checks, cascading FK and ordering index."""
    op.create_table(
        "chat_todos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column("emoji", sa.String(8), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="ck_chat_todos_status"),
        sa.CheckConstraint("position >= 0", name="ck_chat_todos_position"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_chat_todos_workspace", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"], ["chat_conversations.workspace_id", "chat_conversations.id"],
            name="fk_chat_todos_conversation", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_chat_todos_conversation_position", "chat_todos", ["conversation_id", "position"])


def downgrade() -> None:
    """Drop the todos table (data is conversation-scoped working state, safe to discard)."""
    op.drop_index("ix_chat_todos_conversation_position", table_name="chat_todos")
    op.drop_table("chat_todos")
