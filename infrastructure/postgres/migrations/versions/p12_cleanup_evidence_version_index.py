"""Index cleanup evidence references by document version for legacy-row receipt fallback lookups.

``cleanup_evidence_version_document`` resolves a legacy row's Document by version identity while the
Documents worker holds the global privacy lock; without this index each lookup scans the largest
receipt table. Index only: no data is rewritten.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "p12_evidence_version_index"
down_revision: str | Sequence[str] | None = "p12_source_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_document_cleanup_evidence_document_version_id"
_TABLE = "document_cleanup_evidence_references"


def upgrade() -> None:
    """Create the document_version_id lookup index."""
    op.create_index(_INDEX, _TABLE, ["document_version_id"])


def downgrade() -> None:
    """Drop the document_version_id lookup index."""
    op.drop_index(_INDEX, table_name=_TABLE)
