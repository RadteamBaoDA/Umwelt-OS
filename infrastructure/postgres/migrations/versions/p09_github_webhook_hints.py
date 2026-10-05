"""Add durable, bounded GitHub webhook receipt, fanout and source hint ledgers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p09_github_webhook_hints"
down_revision: str | Sequence[str] | None = "p09_github_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create replay-safe digests, bounded fanout-page admissions, source hints, and locked capacity counters."""
    op.create_table(
        "github_webhook_capacity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1 AND digest_count BETWEEN 0 AND 100000 AND pending_count BETWEEN 0 AND 100000", name="ck_github_webhook_capacity_bounds"),
    )
    op.execute(sa.text("INSERT INTO github_webhook_capacity (id, digest_count, pending_count) VALUES (1, 0, 0)"))
    op.create_table(
        "github_webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receiver_revision", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64)),
        sa.Column("app_id", sa.String(20)),
        sa.Column("installation_id", sa.String(20)),
        sa.Column("repository_id", sa.String(20)),
        sa.Column("targets", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details_scrubbed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("receiver_revision", "delivery_id", name="uq_github_webhook_delivery_namespace_id"),
        sa.CheckConstraint("length(raw_sha256) = 64 AND raw_sha256 !~ '[^0-9a-f]'", name="ck_github_webhook_delivery_digest"),
        sa.CheckConstraint("length(delivery_id) BETWEEN 1 AND 128 AND jsonb_array_length(targets) <= 100", name="ck_github_webhook_delivery_bounds"),
        sa.CheckConstraint("disposition IN ('received', 'ignored', 'ping')", name="ck_github_webhook_delivery_disposition"),
    )
    op.create_index("ix_github_webhook_delivery_retention", "github_webhook_deliveries", ["detail_expires_at"])
    op.create_table(
        "github_webhook_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("github_webhook_deliveries.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("binding_cursor", sa.String(512)),
        sa.Column("fanout_page", postgresql.JSONB()),
        sa.Column("capacity_reserved", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('pending', 'dispatched', 'needs_attention', 'complete')", name="ck_github_webhook_outbox_state"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_github_webhook_outbox_attempts"),
        sa.CheckConstraint("fanout_page IS NULL OR CASE WHEN jsonb_typeof(fanout_page) = 'object' AND jsonb_typeof(fanout_page -> 'bindings') = 'array' AND jsonb_typeof(fanout_page -> 'admissions') = 'array' THEN jsonb_array_length(fanout_page -> 'bindings') <= 50 AND jsonb_array_length(fanout_page -> 'admissions') <= 5000 AND octet_length(fanout_page::text) <= 2097152 ELSE false END", name="ck_github_webhook_outbox_fanout_page_bounds"),
    )
    op.create_index("ix_github_webhook_outbox_due", "github_webhook_outbox", ["state", "next_attempt_at"])
    op.create_table(
        "github_source_hints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.String(20), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("resource", sa.String(16), nullable=False),
        sa.Column("locator_kind", sa.String(24), nullable=False),
        sa.Column("locator", sa.String(256), nullable=False),
        sa.Column("intent", sa.String(24), nullable=False),
        sa.Column("dirty_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_revision", sa.Integer()),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acknowledged_batch_id", postgresql.UUID(as_uuid=True)),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("capacity_reserved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reconcile_page", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_id", "resource", "locator_kind", "locator", name="uq_github_source_hint_target"),
        sa.CheckConstraint("dirty_revision > 0 AND attempts BETWEEN 0 AND 5", name="ck_github_source_hint_revision_attempts"),
        sa.CheckConstraint("state IN ('pending', 'dispatched', 'accepted_ingestion', 'completed', 'ignored', 'paused', 'visibility_unverified', 'needs_attention', 'capacity_deferred')", name="ck_github_source_hint_state"),
    )
    op.create_index("ix_github_source_hint_due", "github_source_hints", ["state", "next_attempt_at"])
    op.create_index("ix_github_source_hint_source", "github_source_hints", ["source_id", "state", "updated_at"])


def downgrade() -> None:
    """Remove the unshipped GitHub hint ledgers without altering S1 history."""
    op.drop_index("ix_github_source_hint_source", table_name="github_source_hints")
    op.drop_index("ix_github_source_hint_due", table_name="github_source_hints")
    op.drop_table("github_source_hints")
    op.drop_index("ix_github_webhook_outbox_due", table_name="github_webhook_outbox")
    op.drop_table("github_webhook_outbox")
    op.drop_index("ix_github_webhook_delivery_retention", table_name="github_webhook_deliveries")
    op.drop_table("github_webhook_deliveries")
    op.drop_table("github_webhook_capacity")
