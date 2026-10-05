"""Persist native provider credentials and pre-fetch collection lease ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r13_native_provider_collection"
down_revision: str | Sequence[str] | None = "p08_notification_params"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create native credential storage and add nullable collection lease ownership state.

    This unshipped revision creates historical source identity separately from
    the unique active bot reservation. It performs no identity backfill.
    """
    op.create_table(
        "connector_native_credentials",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), server_default="telegram", nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=True),
        sa.Column("token_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("bound_bot_id", sa.String(length=20), nullable=True),
        sa.Column("verified_bot_id", sa.String(length=20), nullable=True),
        sa.Column("state", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint(
            "verified_bot_id", name="uq_connector_native_credentials_verified_bot"
        ),
        sa.CheckConstraint(
            "provider = 'telegram'", name="ck_connector_native_credentials_provider"
        ),
        sa.CheckConstraint(
            "source_generation > 0 AND configuration_revision > 0",
            name="ck_connector_native_credentials_fences",
        ),
        sa.CheckConstraint(
            "verified_bot_id IS NULL OR (bound_bot_id IS NOT NULL AND verified_bot_id = bound_bot_id)",
            name="ck_connector_native_credentials_verified_identity",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'revoked', 'reconciliation_required')",
            name="ck_connector_native_credentials_state",
        ),
        sa.CheckConstraint(
            "state != 'ready' OR (encrypted_token IS NOT NULL AND token_fingerprint IS NOT NULL "
            "AND bound_bot_id IS NOT NULL AND verified_bot_id IS NOT NULL "
            "AND validated_at IS NOT NULL)",
            name="ck_connector_native_credentials_ready_binding",
        ),
        sa.CheckConstraint(
            "state != 'revoked' OR (encrypted_token IS NULL AND token_fingerprint IS NULL)",
            name="ck_connector_native_credentials_revoked_secret",
        ),
    )
    op.add_column(
        "source_ingestion_state",
        sa.Column("collection_lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_source_ingestion_state_single_lease_owner",
        "source_ingestion_state",
        "NOT (lease_run_id IS NOT NULL AND collection_lease_token IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_source_ingestion_state_collection_lease_expiry",
        "source_ingestion_state",
        "collection_lease_token IS NULL OR lease_expires_at IS NOT NULL",
    )


def downgrade() -> None:
    """Remove only R13 schema; disable and drain routes/workflows first.

    This unshipped revision has not been executed. Downgrade would discard
    credentials, historical bot binding, and active lease authority; it is not
    a data-preserving rollback.
    """
    # Downgrade discards credentials and collection ownership, so their writers must already be disabled and drained.
    op.drop_constraint(
        "ck_source_ingestion_state_collection_lease_expiry",
        "source_ingestion_state",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_ingestion_state_single_lease_owner",
        "source_ingestion_state",
        type_="check",
    )
    op.drop_column("source_ingestion_state", "collection_lease_token")
    op.drop_table("connector_native_credentials")
