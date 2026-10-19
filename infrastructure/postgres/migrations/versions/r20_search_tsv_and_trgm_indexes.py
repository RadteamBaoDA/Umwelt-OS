"""Add functional GIN full-text search index on document_chunks and trigram GIN index on entities.

Builds both indexes concurrently inside autocommit_block so active ingestion and entity resolution
queries do not experience table-level write locks during migration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "r20_search_tsv_and_trgm_indexes"
down_revision: str | Sequence[str] | None = "r19_db_indexes_and_tuning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_document_chunks_content_tsv",
        "document_chunks",
        "USING gin (to_tsvector('simple', content))",
    ),
    (
        "ix_entities_normalized_name_trgm",
        "entities",
        "USING gin (normalized_name gin_trgm_ops)",
    ),
)


def upgrade() -> None:
    """Enable pg_trgm and build functional FTS and trigram GIN indexes concurrently."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        for name, table, tail in _INDEXES:
            # Drop any left-over INVALID index from an interrupted concurrent build before creating.
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}")


def downgrade() -> None:
    """Drop the functional FTS and trigram GIN indexes concurrently; retain pg_trgm extension."""
    with op.get_context().autocommit_block():
        for name, _table, _tail in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

