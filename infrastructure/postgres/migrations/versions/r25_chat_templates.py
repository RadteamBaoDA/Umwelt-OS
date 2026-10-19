"""Add owner-saved chat widget templates and the per-conversation pending template pointer.

Chained after r24_viz_router_settings. ``chat_templates`` is owner/workspace scoped (principal FK, RESTRICT);
``chat_conversations.pending_template`` is a nullable id (user template UUID or built-in seed id).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r25_chat_templates"
down_revision: str | Sequence[str] | None = "r24_viz_router_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create chat_templates with its version check, principal FK and owner index; add the pending pointer."""
    op.create_table(
        "chat_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("data_description", sa.String(500), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1", name="ck_chat_templates_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_chat_templates_principal", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_chat_templates_owner_created", "chat_templates", ["workspace_id", "actor_user_id", "created_at"])
    op.add_column("chat_conversations", sa.Column("pending_template", sa.String(64), nullable=True))


def downgrade() -> None:
    """Drop the pending pointer, then the templates table and its index."""
    op.drop_column("chat_conversations", "pending_template")
    op.drop_index("ix_chat_templates_owner_created", table_name="chat_templates")
    op.drop_table("chat_templates")
