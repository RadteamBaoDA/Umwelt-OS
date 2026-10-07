"""Backfill documents.language from current-version provenance and index it for the feed filter."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r15_document_language_backfill"
down_revision: str | Sequence[str] | None = "p12_evidence_version_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 1000
# Frozen copy of modules.knowledge.documents.public.FEED_LANGUAGES at the time of this migration.
LANGUAGES = ("en", "vi", "fr", "de", "es", "pt", "it", "ru", "ja", "ko", "zh", "id", "th")

_NEXT_IDS = sa.text(
    "SELECT id FROM documents WHERE language IS NULL AND (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid)) "
    "ORDER BY id LIMIT :batch"
)
_UPDATE = sa.text(
    """
    UPDATE documents d SET language = sub.lang
    FROM (
        SELECT d2.id,
               lower(split_part(replace(btrim(p.provenance -> 'metadata' ->> 'language'), '_', '-'), '-', 1)) AS lang
        FROM documents d2
        JOIN document_versions v ON v.document_id = d2.id AND v.version_number = d2.current_version
        JOIN normalized_version_provenance p ON p.document_version_id = v.id
        WHERE d2.id = ANY(:ids)
    ) sub
    WHERE d.id = sub.id AND d.language IS NULL AND sub.lang IN :languages
    """
).bindparams(
    sa.bindparam("ids", type_=postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
    sa.bindparam("languages", expanding=True),
)


def upgrade() -> None:
    """Fill NULL languages in keyset batches (own commits), then build the index concurrently."""
    # Autocommit so each batch commits and releases row locks; the table stays writable throughout.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        last = None
        # Offline (--sql) mode cannot read rows; the batched backfill only runs against a live database.
        while not op.get_context().as_sql:
            ids = bind.execute(_NEXT_IDS, {"last": last, "batch": BATCH}).scalars().all()
            if not ids:
                break
            bind.execute(_UPDATE, {"ids": ids, "languages": list(LANGUAGES)})
            last = str(ids[-1])
        # (language, created_at, id): equality on language plus the feed's created_at/id ORDER BY.
        # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would keep; drop first.
        op.drop_index(
            "ix_documents_language_created_at_id", table_name="documents",
            postgresql_concurrently=True, if_exists=True,
        )
        op.create_index(
            "ix_documents_language_created_at_id", "documents", ["language", "created_at", "id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the index; backfilled values are indistinguishable from ingest-written ones, so keep them."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_documents_language_created_at_id", table_name="documents",
            postgresql_concurrently=True, if_exists=True,
        )
