"""Own durable temporal mappings, detached cleanup receipts and relationship transaction history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p05_temporal_sync"
down_revision: str | Sequence[str] | None = "p05_timeline_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create exact-ID owner ledgers without cascading source/version foreign keys; no provider operations."""
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    aware = sa.DateTime(timezone=True)
    op.create_table("temporal_allocations",
        sa.Column("source_id", uuid, primary_key=True), sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("next_bucket", sa.Integer(), nullable=False))
    op.create_table("temporal_partitions",
        sa.Column("id", uuid, primary_key=True), sa.Column("source_id", uuid, nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("reservations", sa.Integer(), nullable=False), sa.Column("evidence_reservations", sa.Integer(), nullable=False),
        sa.Column("sealed", sa.Boolean(), nullable=False), sa.Column("lease_token", uuid),
        sa.Column("lease_expires_at", aware), sa.Column("uncertain_operation_id", uuid),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "generation", "ordinal"),
        sa.CheckConstraint("reservations BETWEEN 0 AND 100 AND evidence_reservations BETWEEN 0 AND 100", name="ck_temporal_partition_bound"))
    op.create_index("ix_temporal_partitions_source_id", "temporal_partitions", ["source_id"])
    op.create_table("temporal_mappings",
        sa.Column("id", uuid, primary_key=True), sa.Column("episode_id", uuid, nullable=False, unique=True),
        sa.Column("partition_id", uuid, nullable=False), sa.Column("source_id", uuid, nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False), sa.Column("document_id", uuid, nullable=False),
        sa.Column("document_version_id", uuid, nullable=False), sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False), sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column("desired_digest", sa.String(64), nullable=False), sa.Column("applied_digest", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("error_code", sa.String(64)),
        sa.Column("tombstoned", sa.Boolean(), nullable=False), sa.Column("applied_at", aware),
        sa.Column("canonical_state", jsonb, nullable=False), sa.Column("embedding_identity", jsonb, nullable=False),
        sa.Column("external_state", sa.String(16), nullable=False),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_version_id", "source_generation"))
    for column in ("partition_id", "source_id", "document_id", "document_version_id", "status"):
        op.create_index("ix_temporal_mappings_" + column, "temporal_mappings", [column])
    op.create_table("temporal_supports",
        sa.Column("mapping_id", uuid, primary_key=True), sa.Column("document_version_id", uuid, primary_key=True),
        sa.Column("chunk_id", uuid, primary_key=True), sa.Column("document_id", uuid, nullable=False),
        sa.Column("source_id", uuid, nullable=False), sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False))
    op.create_index("ix_temporal_supports_source_id", "temporal_supports", ["source_id"])
    op.create_table("temporal_operations",
        sa.Column("id", uuid, primary_key=True), sa.Column("mapping_id", uuid, nullable=False),
        sa.Column("partition_id", uuid, nullable=False), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False), sa.Column("desired_digest", sa.String(64), nullable=False),
        sa.Column("receipt_token", uuid, nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", aware, nullable=False), sa.Column("lease_owner", uuid), sa.Column("lease_expires_at", aware),
        sa.Column("dispatched_at", aware), sa.Column("dispatch_deadline", aware),
        sa.Column("cessation_verified_at", aware), sa.Column("cessation_reason", sa.String(64)),
        sa.Column("dispatch_server_run_id", sa.String(64)), sa.Column("dispatch_client_id", sa.BigInteger()),
        sa.Column("cleanup_completed_at", aware),
        sa.Column("error_code", sa.String(64)), sa.Column("dependency_fingerprint", sa.String(64)),
        sa.Column("replacement_created_at", aware, nullable=False),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()))
    for column in ("mapping_id", "partition_id", "status"):
        op.create_index("ix_temporal_operations_" + column, "temporal_operations", [column])
    op.create_table("temporal_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", uuid, nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", jsonb, nullable=False), sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("operation_id", "sequence"))
    op.create_index("ix_temporal_receipts_operation_id", "temporal_receipts", ["operation_id"])
    op.create_table("temporal_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("canonical_id", uuid, nullable=False),
        sa.Column("revision", sa.Integer()), sa.Column("fingerprint", sa.String(64)),
        sa.Column("changed_fields", jsonb, nullable=False), sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False), sa.Column("support", jsonb, nullable=False),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()))
    for column in ("kind", "canonical_id"):
        op.create_index("ix_temporal_changes_" + column, "temporal_changes", [column])
    op.create_table("temporal_reconcile_runs",
        sa.Column("id", uuid, primary_key=True), sa.Column("scope", jsonb, nullable=False),
        sa.Column("upper_mapping_id", uuid), sa.Column("cursor", uuid), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False), sa.Column("queued", sa.Integer(), nullable=False),
        sa.Column("converged", sa.Integer(), nullable=False), sa.Column("blocked", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_temporal_reconcile_runs_status", "temporal_reconcile_runs", ["status"])
    op.create_table("temporal_reconcile_members",
        sa.Column("run_id", uuid, primary_key=True), sa.Column("mapping_id", uuid, primary_key=True),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("desired_digest", sa.String(64), nullable=False),
        sa.Column("tombstoned", sa.Boolean(), nullable=False))
    op.create_table("temporal_rebuild_dependencies",
        sa.Column("operation_id", uuid, primary_key=True), sa.Column("mapping_id", uuid, primary_key=True),
        sa.Column("source_generation", sa.Integer(), nullable=False), sa.Column("effect_ids", jsonb, nullable=False),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()))
    op.create_table("temporal_dispatches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", uuid, nullable=False), sa.Column("group_id", sa.String(200), nullable=False),
        sa.Column("lease_owner", uuid, nullable=False),
        sa.Column("server_run_id", sa.String(40), nullable=False), sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", aware), sa.Column("cessation_verified_at", aware),
        sa.Column("cessation_reason", sa.String(64)),
        sa.Column("created_at", aware, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("operation_id", "server_run_id", "client_id"))
    op.create_index("ix_temporal_dispatches_operation_id", "temporal_dispatches", ["operation_id"])
    op.create_table("relationship_snapshot_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("relationship_id", uuid, nullable=False),
        sa.Column("recorded_at", aware, nullable=False, server_default=sa.func.now()),
        sa.Column("state", jsonb, nullable=False), sa.Column("support", jsonb, nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_relationship_snapshot_history_relationship", "relationship_snapshot_history",
                    ["relationship_id", "recorded_at", "id"])


def downgrade() -> None:
    """Remove only this revision's temporal owner ledgers in reverse order; retained canonical tables remain."""
    for table in ("relationship_snapshot_history", "temporal_dispatches", "temporal_rebuild_dependencies", "temporal_reconcile_members", "temporal_reconcile_runs", "temporal_changes", "temporal_receipts",
                  "temporal_operations", "temporal_supports", "temporal_mappings", "temporal_partitions", "temporal_allocations"):
        op.drop_table(table)
