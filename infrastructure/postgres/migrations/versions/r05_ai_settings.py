"""Persist the single owner's AI gateway settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r05_ai_settings"
down_revision: str | Sequence[str] | None = "r03_connector_provisioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create revisioned owner AI settings and record the gateway identity on search index generations."""
    op.create_table(
        "ai_settings",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("privacy", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("chat_alias", sa.String(length=32), server_default="reasoning-large", nullable=False),
        sa.Column("brief_alias", sa.String(length=32), server_default="reasoning-small", nullable=False),
        sa.Column("web_search_provider", sa.String(length=32), server_default="none", nullable=False),
        sa.Column("web_search_endpoint", sa.Text()),
        sa.Column("web_search_api_key_ciphertext", sa.Text()),
        sa.Column("request_timeout_seconds", sa.Integer(), server_default="20", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("owner_id = 1", name="ck_ai_settings_single_owner"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_ai_settings_revision_positive"),
        sa.CheckConstraint("request_timeout_seconds BETWEEN 5 AND 180", name="ck_ai_settings_timeout"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.add_column("search_index_generations", sa.Column("gateway_identity", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Remove the search gateway identity field and owner AI settings table."""
    op.drop_column("search_index_generations", "gateway_identity")
    op.drop_table("ai_settings")
