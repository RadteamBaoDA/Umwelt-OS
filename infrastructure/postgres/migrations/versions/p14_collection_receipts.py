"""Add collection acceptance receipts, conditional-GET validators and the continuation checkpoint."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_collection_receipts"
down_revision: str | Sequence[str] | None = "p14_provider_terms_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "ingestion_collection_receipts"


def upgrade() -> None:
    """Create the receipt table and the nullable source-state columns (no backfill)."""
    op.create_table(
        _T,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer()),
        sa.Column("admission_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("payload_digest", sa.String(64)),
        sa.Column("cursor_before", sa.Text()),
        sa.Column("cursor_after", sa.Text()),
        sa.Column("coverage", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("etag", sa.String(512)),
        sa.Column("last_modified", sa.String(128)),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id"),
        sa.CheckConstraint("outcome IN ('accepted', 'no_changes')", name="ck_ingestion_collection_receipts_outcome"),
        sa.CheckConstraint("coverage IN ('complete', 'partial')", name="ck_ingestion_collection_receipts_coverage"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_ingestion_collection_receipts_workspace", ondelete="RESTRICT"),
    )
    op.create_index("ix_ingestion_collection_receipts_source", _T, ["workspace_id", "source_id", "accepted_at"])
    op.create_index("ix_ingestion_collection_receipts_retain", _T, ["retain_until"])
    op.add_column("source_ingestion_state", sa.Column("etag", sa.String(512)))
    op.add_column("source_ingestion_state", sa.Column("last_modified", sa.String(128)))
    op.add_column("source_ingestion_state", sa.Column("validators_revision", sa.Integer()))
    op.add_column("source_ingestion_state", sa.Column("continuation_state", sa.Text()))


def downgrade() -> None:
    """Drop the state columns and the receipt table."""
    for column in ("continuation_state", "validators_revision", "last_modified", "etag"):
        op.drop_column("source_ingestion_state", column)
    op.drop_index("ix_ingestion_collection_receipts_retain", table_name=_T)
    op.drop_index("ix_ingestion_collection_receipts_source", table_name=_T)
    op.drop_table(_T)
