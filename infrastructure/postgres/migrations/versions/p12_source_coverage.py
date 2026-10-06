"""Add Source-wide historical Documents coverage indexes and Source-local Memory coverage state.

One serialized descendant of ``p12_copied_stage_cleanup``. Truthful defaults: every operation
starts its new Source Memory stage ``queued`` (never clean) and no historical row is rewritten
except that (marked by ``coverage_reopened``) a purge operation previously advertised ``succeeded`` is reopened to ``running``,
because full-copy success now also requires the new coverage. ``documents_status`` (the accepted
canonical milestone) is never changed. Nothing here backfills or executes cleanup.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "p12_source_coverage"
down_revision: str | Sequence[str] | None = "p12_copied_stage_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "source_purge_operations"


def upgrade() -> None:
    """Add Source Memory stage columns, reopen succeeded operations, and add coverage indexes."""
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(_TABLE, sa.Column("memory_status", sa.String(16), server_default="queued", nullable=False))
    op.add_column(_TABLE, sa.Column("memory_error_code", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("memory_cursor", jsonb, nullable=True))
    op.add_column(_TABLE, sa.Column("memory_unresolved_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column(_TABLE, sa.Column("memory_cache_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    # Marks exactly the rows the reopen below touches, so downgrade can reverse only them.
    op.add_column(_TABLE, sa.Column("coverage_reopened", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_check_constraint(
        "ck_source_purge_operations_memory_status", _TABLE,
        "memory_status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_source_purge_operations_memory_cursor_bound", _TABLE,
        "memory_cursor IS NULL OR octet_length(memory_cursor::text) <= 4096",
    )
    op.create_check_constraint(
        "ck_source_purge_operations_memory_unresolved", _TABLE, "memory_unresolved_count >= 0",
    )
    # Pre-integration success never covered Source-local Memory: reopen it, keep documents_status.
    op.execute(
        "UPDATE source_purge_operations SET status = 'running', coverage_reopened = true, error_code = NULL, "
        "pending_owner_codes = '[\"memory\"]'::jsonb WHERE status = 'succeeded'"
    )
    op.create_index("ix_source_purge_operations_source_id", _TABLE, ["source_id", "id"])
    op.create_index(
        "ix_source_purge_operations_memory_reconcile", _TABLE, ["id"],
        postgresql_where=sa.text(
            "documents_status = 'deleted' AND (memory_status IN ('queued', 'running') "
            "OR memory_cache_pending OR (memory_status = 'failed' AND memory_error_code "
            "NOT IN ('evidence_identity_unavailable', 'legacy_provenance_unresolved')))"
        ),
    )
    # Exact-source aggregate over every retained cleanup receipt, including NULL/older linkage.
    op.create_index("ix_document_cleanup_source_id_id", "document_cleanup_operations", ["source_id", "id"])
    # Owner-local exact provenance equality and candidate linkage for the whole-Source sweep.
    op.create_index(
        "ix_memories_provenance_source_id", "memories", [sa.text("(provenance ->> 'source_id')"), "id"],
    )
    op.create_index("ix_memories_candidate_id", "memories", ["candidate_id"])
    op.create_index(
        "ix_memory_candidates_provenance_source_id", "memory_candidates",
        [sa.text("(provenance ->> 'source_id')"), "id"],
    )


def downgrade() -> None:
    """Remove the unshipped coverage schema; reopened operations not yet re-settled return to succeeded."""
    # Exact reversal: only marked rows still running; re-settled (succeeded/failed) rows are kept.
    op.execute(
        "UPDATE source_purge_operations SET status = 'succeeded', pending_owner_codes = '[]'::jsonb "
        "WHERE coverage_reopened AND status = 'running'"
    )
    op.drop_index("ix_memory_candidates_provenance_source_id", table_name="memory_candidates")
    op.drop_index("ix_memories_candidate_id", table_name="memories")
    op.drop_index("ix_memories_provenance_source_id", table_name="memories")
    op.drop_index("ix_document_cleanup_source_id_id", table_name="document_cleanup_operations")
    op.drop_index("ix_source_purge_operations_memory_reconcile", table_name=_TABLE)
    op.drop_index("ix_source_purge_operations_source_id", table_name=_TABLE)
    op.drop_constraint("ck_source_purge_operations_memory_unresolved", _TABLE, type_="check")
    op.drop_constraint("ck_source_purge_operations_memory_cursor_bound", _TABLE, type_="check")
    op.drop_constraint("ck_source_purge_operations_memory_status", _TABLE, type_="check")
    op.drop_column(_TABLE, "coverage_reopened")
    op.drop_column(_TABLE, "memory_cache_pending")
    op.drop_column(_TABLE, "memory_unresolved_count")
    op.drop_column(_TABLE, "memory_cursor")
    op.drop_column(_TABLE, "memory_error_code")
    op.drop_column(_TABLE, "memory_status")
