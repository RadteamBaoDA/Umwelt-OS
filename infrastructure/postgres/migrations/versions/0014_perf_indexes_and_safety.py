"""Performance indexes, stored lexical ``tsvector`` column and append-heavy autovacuum tuning.

Online-safety pattern (see ``core.migration_support``): every index is created/dropped with
``CREATE/DROP INDEX CONCURRENTLY IF [NOT] EXISTS`` inside ``autocommit_block`` (no write-blocking lock; an
invalid leftover of an interrupted earlier run is dropped first), and ``env.py`` applies ``lock_timeout`` /
``statement_timeout`` to the migration connection so a DDL step that cannot get its lock fails fast.

What this revision does
-----------------------
1. **Stored generated column** ``document_chunks.content_tsv tsvector GENERATED ALWAYS AS
   (to_tsvector('simple', content)) STORED`` plus a GIN index; lexical search now matches and ranks
   through it (identical scores: it is the same expression).

   .. warning:: LOCK / REWRITE COST. Adding a STORED generated column rewrites ``document_chunks`` while
      holding ``ACCESS EXCLUSIVE`` on it: reads and writes of chunks block for roughly the time needed to
      re-parse every chunk (order of 1-2 minutes per million average chunks, plus WAL for the rewritten
      table) and disk usage temporarily doubles. This is accepted for this release; run it in a
      maintenance window, ensure free disk >= table size, and raise ``UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS``
      if the table is very large (default 1 h). ``lock_timeout`` (default 5 s) makes the step fail fast and
      retryable instead of queueing behind long transactions. The GIN build afterwards is CONCURRENT.

2. **Index clean-up.** Dropped (all CONCURRENTLY): ``ix_document_chunks_simple_fts`` and
   ``ix_document_chunks_content_tsv`` (identical expression GIN indexes, both superseded by the column index)
   and ``ix_relationship_evidence_document`` (exact duplicate of the model-declared
   ``ix_relationship_evidence_document_id``). ``chat_stream_events`` (``ix_*_response_id`` / ``ix_*_seq``,
   redundant with ``UNIQUE(response_id, seq)``) and ``realtime_replay_events`` indexes were already removed
   by the ``0001`` baseline, so nothing is left to drop there.
3. **Foreign-key indexes** for FK columns without a leading index on delete/cascade/erasure/cleanup paths
   (``scripts/list_unindexed_fks.py`` lists candidates from the migration DDL). Parent tables ``workspaces``
   and ``owner`` are excluded (single-row parents, deleted only by account erasure) as is ``workspace_id``
   (one workspace per deployment never narrows a lookup). Names are ``ix_fk_<table>_<column>``.
4. **Partial status indexes** replacing low-selectivity single-column ones: active chat runs, open temporal
   operations, pending/running reconcile runs, plus ``ix_event_outbox_failed_created`` for the retention purge.
5. **Per-table autovacuum / analyze scale factors** for append-heavy tables (metadata-only,
   ``SHARE UPDATE EXCLUSIVE``).

Not done on purpose: time-partitioning ``chat_stream_events`` / ``realtime_replay_events``. Partitioning
requires the partition key in every unique/primary key; ``chat_stream_events`` carries ``UNIQUE(event_id)`` and
``UNIQUE(response_id, seq)`` (used for idempotent upserts) and ``realtime_replay_events`` a
``(workspace_id, user_id, sequence)`` primary key, none of which can include ``created_at`` without changing
their guarantees. Both tables already have bounded retention jobs; see ``docs/operations.md``.
"""

from collections.abc import Sequence

from alembic import op

from core.migration_support import create_index_concurrently, drop_index_concurrently

revision: str = "0014_perf_indexes_and_safety"
down_revision: str | Sequence[str] | None = "0013_chat_sql_reader_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, partial predicate or None). Nullable, mostly-NULL columns get a partial index.
_FK_INDEXES: tuple[tuple[str, str, str | None], ...] = (
    ("entity_aliases", "source_id", None),
    ("entity_alias_evidence", "membership_id", None),
    ("entity_correction_decisions", "entity_id", None),
    ("entity_correction_decisions", "membership_id", None),
    ("github_oauth_attempts", "source_id", None),
    ("news_observations", "document_id", None),
    ("normalized_document_identities", "document_id", None),
    ("observations", "document_id", None),
    ("observation_normalizations", "source_id", None),
    ("observation_normalizations", "run_id", None),
    ("source_ingestion_state", "lease_run_id", "lease_run_id IS NOT NULL"),
    ("timeline_event_evidence", "document_id", None),
    ("timeline_extraction_work", "document_id", None),
    ("timeline_extraction_work", "source_id", None),
    ("timeline_participant_evidence", "event_evidence_id", None),
    ("connector_collection_requests", "source_id", None),
    ("agent_evidence_cleanups", "run_id", None),
    ("dashboard_groups", "dashboard_id", None),
    ("gadget_instances", "dashboard_id", None),
    ("gadget_instances", "definition_id", None),
    ("gadget_instances", "group_id", "group_id IS NOT NULL"),
    ("mcp_capability_grants", "capability_id", None),
    ("memories", "superseded_by_id", "superseded_by_id IS NOT NULL"),
)

# (new index, table, column, predicate, old index replaced).
_PARTIAL_STATUS_INDEXES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "ix_chat_response_runs_active_status", "chat_response_runs", "status",
        "status IN ('pending', 'streaming', 'awaiting_input')", "ix_chat_response_runs_status",
    ),
    (
        "ix_temporal_operations_open_status", "temporal_operations", "status",
        "status IN ('pending', 'running', 'blocked', 'reconcile_needed')", "ix_temporal_operations_status",
    ),
    (
        "ix_temporal_reconcile_runs_open_status", "temporal_reconcile_runs", "status",
        "status IN ('pending', 'running')", "ix_temporal_reconcile_runs_status",
    ),
)

_DUPLICATE_OR_SUPERSEDED = (
    "ix_document_chunks_simple_fts",
    "ix_document_chunks_content_tsv",
    "ix_relationship_evidence_document",
)

# table -> storage parameters. Scale factors are fractions of live rows that trigger (auto)vacuum / analyze.
_AUTOVACUUM: dict[str, str] = {
    "event_outbox": "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02, "
                    "autovacuum_vacuum_insert_scale_factor = 0.05",
    "chat_stream_events": "autovacuum_analyze_scale_factor = 0.02",  # vacuum factors were set in 0001
    "realtime_replay_events": "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02, "
                              "autovacuum_vacuum_insert_scale_factor = 0.05",
    "source_observations": "autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05, "
                           "autovacuum_vacuum_insert_scale_factor = 0.05",
    "system_health_samples": "autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05, "
                             "autovacuum_vacuum_insert_scale_factor = 0.05",
}
_AUTOVACUUM_RESET: dict[str, str] = {
    "event_outbox": "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
                    "autovacuum_vacuum_insert_scale_factor",
    "chat_stream_events": "autovacuum_analyze_scale_factor",
    "realtime_replay_events": "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
                              "autovacuum_vacuum_insert_scale_factor",
    "source_observations": "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
                           "autovacuum_vacuum_insert_scale_factor",
    "system_health_samples": "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
                             "autovacuum_vacuum_insert_scale_factor",
}


def _fk_index_name(table: str, column: str) -> str:
    """Return the ``ix_fk_<table>_<column>`` name (Postgres truncates identifiers at 63 characters)."""
    return f"ix_fk_{table}_{column}"[:63]


def upgrade() -> None:
    """Add the stored tsvector column, then create/drop all indexes online and tune autovacuum.

    Step order: (1) transactional DDL -- generated column (table rewrite, ACCESS EXCLUSIVE; see module
    docstring) and storage parameters; (2) autocommit blocks -- GIN index first so search never lacks a lexical
    index, then FK and partial indexes, and only then the superseded/duplicate indexes are dropped.
    """
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    )
    for table, parameters in _AUTOVACUUM.items():
        op.execute(f"ALTER TABLE {table} SET ({parameters})")

    create_index_concurrently("ix_document_chunks_content_tsv_gin", "document_chunks", "USING gin (content_tsv)")
    for table, column, predicate in _FK_INDEXES:
        where = f" WHERE {predicate}" if predicate else ""
        create_index_concurrently(_fk_index_name(table, column), table, f"({column}){where}")
    for name, table, column, predicate, _old in _PARTIAL_STATUS_INDEXES:
        create_index_concurrently(name, table, f"({column}) WHERE {predicate}")
    # Terminal-failed rows are purged by retention too (delivered rows already have a partial index).
    create_index_concurrently(
        "ix_event_outbox_failed_created", "event_outbox", "(created_at) WHERE status = 'failed'",
    )

    for _name, _table, _column, _predicate, old in _PARTIAL_STATUS_INDEXES:
        drop_index_concurrently(old)
    for name in _DUPLICATE_OR_SUPERSEDED:
        drop_index_concurrently(name)


def downgrade() -> None:
    """Restore the previous index set and drop the generated column (inverse order of ``upgrade``).

    Recreates both expression GIN indexes (``ix_document_chunks_simple_fts`` and ``ix_document_chunks_content_tsv``, the 0013 set) and the single-column status indexes concurrently, drops the new
    indexes, resets the storage parameters and finally drops ``content_tsv`` (a cheap catalog change, but it
    takes ``ACCESS EXCLUSIVE`` briefly). Lexical search must be the previous release when this runs.
    """
    create_index_concurrently(
        "ix_document_chunks_simple_fts", "document_chunks", "USING gin (to_tsvector('simple', content))",
    )
    create_index_concurrently(
        "ix_document_chunks_content_tsv", "document_chunks", "USING gin (to_tsvector('simple', content))",
    )
    create_index_concurrently("ix_relationship_evidence_document", "relationship_evidence", "(document_id)")
    for _name, table, column, _predicate, old in _PARTIAL_STATUS_INDEXES:
        create_index_concurrently(old, table, f"({column})")

    drop_index_concurrently("ix_event_outbox_failed_created")
    for name, _table, _column, _predicate, _old in _PARTIAL_STATUS_INDEXES:
        drop_index_concurrently(name)
    for table, column, _predicate in _FK_INDEXES:
        drop_index_concurrently(_fk_index_name(table, column))
    drop_index_concurrently("ix_document_chunks_content_tsv_gin")

    for table, parameters in _AUTOVACUUM_RESET.items():
        op.execute(f"ALTER TABLE {table} RESET ({parameters})")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_tsv")
