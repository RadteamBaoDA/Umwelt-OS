"""Add workspace translation settings and the derived translation cache/batch tables.

Reserves the T3 lease/slot/attempt/retention columns. Nothing is backfilled: absent settings
mean disabled/vi/revision 1. down_revision is frozen in the takeover chain (P1 creates its parent).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_translation"
down_revision: str | Sequence[str] | None = "p14_collection_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = "status IN ('pending', 'ready', 'unchanged', 'blocked', 'failed')"
_RESOURCE = "resource_type IN ('news_story', 'daily_brief')"


def _ts(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column:
    """Timezone-aware timestamp column; ``default`` adds now()."""
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=sa.func.now() if default else None)


def upgrade() -> None:
    """Create translation_settings, content_translations, translation_batches and items."""
    op.create_table(
        "translation_settings",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target_language", sa.String(2), nullable=False, server_default="vi"),
        sa.Column("configuration_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="SET NULL")),
        _ts("created_at", default=True), _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_translation_settings_workspace", ondelete="RESTRICT"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_translation_settings_revision"),
        sa.CheckConstraint("target_language IN ('vi', 'en')", name="ck_translation_settings_target"),
    )
    op.create_table(
        "content_translations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_revision", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("visibility_hash", sa.String(64), nullable=False),
        sa.Column("target_language", sa.String(2), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        _ts("lease_expires_at", nullable=True),
        sa.Column("slot_token", postgresql.UUID(as_uuid=True)),
        _ts("slot_expires_at", nullable=True), _ts("next_attempt_at", nullable=True),
        _ts("completed_at", nullable=True), _ts("expires_at"),
        _ts("created_at", default=True), _ts("updated_at", default=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_content_translations_workspace", ondelete="CASCADE"),
        sa.CheckConstraint(_STATUS, name="ck_content_translations_status"),
        sa.CheckConstraint(_RESOURCE, name="ck_content_translations_resource_type"),
        sa.CheckConstraint("target_language IN ('vi', 'en')", name="ck_content_translations_target"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_content_translations_attempts"),
        sa.UniqueConstraint(
            "workspace_id", "actor_user_id", "resource_type", "resource_id", "resource_revision",
            "content_hash", "visibility_hash", "target_language", "config_hash", "prompt_version",
            name="uq_content_translations_fingerprint"),
    )
    op.create_index("ix_content_translations_scope", "content_translations", ["workspace_id", "status", "created_at"])
    op.create_index("ix_content_translations_expiry", "content_translations", ["expires_at"])
    op.create_index("ix_content_translations_lease", "content_translations", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_table(
        "translation_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_language", sa.String(2), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        _ts("expires_at"), _ts("created_at", default=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_translation_batches_workspace", ondelete="CASCADE"),
        sa.CheckConstraint("target_language IN ('vi', 'en')", name="ck_translation_batches_target"),
        sa.CheckConstraint("settings_revision > 0", name="ck_translation_batches_revision"),
    )
    op.create_index("ix_translation_batches_actor", "translation_batches", ["workspace_id", "actor_user_id", "created_at"])
    op.create_index("ix_translation_batches_expiry", "translation_batches", ["expires_at"])
    op.create_table(
        "translation_batch_items",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_revision", sa.String(128), nullable=False),
        sa.Column("translation_id", postgresql.UUID(as_uuid=True)),
        _ts("created_at", default=True),
        sa.ForeignKeyConstraint(["batch_id"], ["translation_batches.id"], name="fk_translation_batch_items_batch", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["translation_id"], ["content_translations.id"], name="fk_translation_batch_items_translation", ondelete="SET NULL"),
        sa.CheckConstraint(_RESOURCE, name="ck_translation_batch_items_resource_type"),
        sa.UniqueConstraint("batch_id", "resource_type", "resource_id", name="uq_translation_batch_items_ref"),
    )
    op.create_index("ix_translation_batch_items_translation", "translation_batch_items", ["translation_id"])


def downgrade() -> None:
    """Drop the translation tables in dependency order."""
    op.drop_table("translation_batch_items")
    op.drop_table("translation_batches")
    op.drop_table("content_translations")
    op.drop_table("translation_settings")
