"""Add FK-cascade/lookup indexes, drop unused/duplicate ones, prune backup activity, tune autovacuum.

Indexes use CONCURRENTLY inside autocommit_block so large tables stay writable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "r19_db_indexes_and_tuning"
down_revision: str | Sequence[str] | None = "r18_auth_session_last_seen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, table, column list / expression tail after table name). Plain btree unless noted.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_search_index_items_chunk_id", "search_index_items", "(chunk_id)"),
    ("ix_news_observations_chunk_id", "news_observations", "(chunk_id)"),
    ("ix_relationship_evidence_document_id", "relationship_evidence", "(document_id)"),
    ("ix_observation_normalizations_document_version_id", "observation_normalizations", "(document_version_id)"),
    ("ix_observation_normalizations_document_id", "observation_normalizations", "(document_id)"),
    ("ix_observations_ingestion_observation_id", "observations", "(ingestion_observation_id)"),
    ("ix_timeline_event_evidence_chunk_id", "timeline_event_evidence", "(chunk_id)"),
    ("ix_document_interactions_document_version_id", "document_interactions", "(document_version_id)"),
    ("ix_chat_response_runs_user_message_id", "chat_response_runs", "(user_message_id)"),
    ("ix_chat_response_runs_assistant_message_id", "chat_response_runs", "(assistant_message_id)"),
    ("ix_chat_message_mutation_receipts_target_message_id", "chat_message_mutation_receipts", "(target_message_id)"),
    ("ix_chat_message_mutation_receipts_result_user_message_id", "chat_message_mutation_receipts", "(result_user_message_id)"),
    ("ix_source_observations_source_provider_record", "source_observations", "(source_id, provider_id, record_hash)"),
    ("ix_temporal_mappings_canonical_state", "temporal_mappings", "USING gin (canonical_state jsonb_path_ops)"),
    (
        "ix_event_outbox_dispatch_created", "event_outbox",
        "(created_at, id) WHERE status IN ('pending', 'queued')",
    ),
    # Retention purges: partial on terminal states so the bounded `id IN (... LIMIT n)` scans stay small.
    (
        "ix_connector_collection_requests_terminal_updated", "connector_collection_requests",
        "(updated_at) WHERE status IN ('succeeded', 'no_changes', 'failed', 'cancelled')",
    ),
    # Reverse of the RESTRICT FK fk_connector_quota_debits_window (PK leads with send_id): serves the purge NOT EXISTS.
    (
        "ix_connector_quota_debits_window", "connector_quota_debits",
        "(provider_id, budget_kind, subject_hash, policy_key, window_start)",
    ),
    ("ix_event_outbox_delivered_created", "event_outbox", "(created_at) WHERE status = 'delivered'"),
    (
        "ix_automation_runs_terminal_finished", "automation_runs",
        "(finished_at) WHERE status IN ('succeeded', 'failed', 'skipped', 'dropped')",
    ),
    (
        "ix_chat_response_runs_terminal_completed", "chat_response_runs",
        "(completed_at) WHERE status IN ('completed', 'cancelled', 'failed')",
    ),
)

# Replay reads always hit the PK prefix (workspace_id, user_id, sequence); chat stream events are
# already covered by uq_chat_stream_events_seq (response_id, seq).
_DROPPED: tuple[tuple[str, str, str], ...] = (
    ("ix_realtime_replay_events_epoch_sequence", "realtime_replay_events", "(epoch, sequence)"),
    ("ix_realtime_replay_events_created_at", "realtime_replay_events", "(created_at)"),
    (
        "ix_w2_realtime_replay_events_work", "realtime_replay_events",
        "(workspace_id, user_id, created_at, sequence)",
    ),
    ("ix_chat_stream_events_response_id", "chat_stream_events", "(response_id)"),
    ("ix_chat_stream_events_seq", "chat_stream_events", "(response_id, seq)"),
)


def upgrade() -> None:
    """Build indexes concurrently, drop redundant ones, prune dead backup rows, tune autovacuum, enable pg_stat_statements."""
    with op.get_context().autocommit_block():
        for name, table, tail in _INDEXES:
            # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would keep.
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}")
        for name, _table, _tail in _DROPPED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    # Only active/uncertain activity is ever read; finish_activity now deletes its row.
    op.execute("DELETE FROM p12_backup_activity WHERE state IN ('finished', 'interrupted')")
    # High-churn append table: vacuum/analyze earlier than the 20% default.
    op.execute(
        "ALTER TABLE chat_stream_events SET "
        "(autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_insert_scale_factor = 0.05)"
    )
    # Skip silently when the library is not preloaded (non-compose Postgres): CREATE EXTENSION
    # itself succeeds without it but the views then error on use, so require the preload setting.
    op.execute(
        """
        DO $$
        BEGIN
            IF current_setting('shared_preload_libraries', true) LIKE '%pg_stat_statements%'
               AND EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements') THEN
                CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_stat_statements not enabled: %', SQLERRM;
        END
        $$
        """
    )


def downgrade() -> None:
    """Restore dropped indexes, drop added ones, reset autovacuum; pruned backup rows and the extension stay."""
    with op.get_context().autocommit_block():
        for name, table, tail in _DROPPED:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}")
        for name, _table, _tail in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(
        "ALTER TABLE chat_stream_events RESET "
        "(autovacuum_vacuum_scale_factor, autovacuum_vacuum_insert_scale_factor)"
    )
