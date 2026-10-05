"""Add immutable source-scoped structured observation revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r14_observations"
down_revision: str | Sequence[str] | None = "r12_dashboard_highlight_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create evidence-linked observation and encrypted provider credential tables."""
    op.add_column("observation_normalizations", sa.Column("selected_current", sa.Boolean(), nullable=True))
    op.create_table(
        "observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_scope_discriminator", sa.String(length=64), nullable=False),
        sa.Column("provider_version", sa.String(length=255), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=True),
        sa.Column("region", sa.String(length=80), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("missing_reason", sa.String(length=64), nullable=True),
        sa.Column("provider_delay_seconds", sa.Integer(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ingestion_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision > 0 AND source_generation > 0", name="ck_observations_revision_fences"),
        sa.CheckConstraint("provider IN ('alpha_vantage', 'open_meteo')", name="ck_observations_provider"),
        sa.CheckConstraint("value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)", name="ck_observations_finite_value"),
        sa.CheckConstraint("latitude IS NULL = (longitude IS NULL)", name="ck_observations_coordinate_pair"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingestion_observation_id"], ["source_observations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", "revision", name="uq_observations_series_revision"),
        sa.UniqueConstraint("source_id", "external_id", "ingestion_observation_id", name="uq_observations_ingestion_acceptance"),
    )
    op.create_index(
        "ix_observations_owner_series_current", "observations",
        ["source_id", "provider", "metric", "symbol", "region", "is_current", "observed_at", "id"],
    )
    op.create_index("ix_observations_document_version", "observations", ["document_version_id"])
    op.create_table(
        "connector_world_credentials",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("provider = 'alpha_vantage'", name="ck_connector_world_credentials_provider"),
        sa.CheckConstraint("source_generation > 0 AND configuration_revision > 0", name="ck_connector_world_credentials_fences"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id"),
    )


def downgrade() -> None:
    """Remove the R14 tables without modifying documents or ingestion history."""
    op.drop_column("observation_normalizations", "selected_current")
    op.drop_table("connector_world_credentials")
    op.drop_index("ix_observations_document_version", table_name="observations")
    op.drop_index("ix_observations_owner_series_current", table_name="observations")
    op.drop_table("observations")
