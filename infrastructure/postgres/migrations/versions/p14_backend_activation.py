"""C4: backend transition, template/credential revisions and native REST credential storage.

Existing rows keep n8n. Rows that were active are marked applied at their current backend revision
but at template revision 0, so they fail closed until a template upgrade installs the current one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_backend_activation"
down_revision: str | Sequence[str] | None = "p14_translation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASES = "transition_phase IN ('idle', 'draining', 'deactivating_old', 'activating_new', 'reconciliation_required')"


def upgrade() -> None:
    """Add the reserved C4 columns and the native REST credential table."""
    t = "connector_provisioning"
    op.add_column(t, sa.Column("applied_backend_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(t, sa.Column("target_backend", sa.String(16)))
    op.add_column(t, sa.Column("transition_phase", sa.String(24), nullable=False, server_default="idle"))
    op.add_column(t, sa.Column("transition_operation_id", postgresql.UUID(as_uuid=True)))
    op.add_column(t, sa.Column("old_workflow_id", sa.String(128)))
    op.add_column(t, sa.Column("template_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(t, sa.Column("applied_template_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(t, sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE connector_provisioning SET applied_backend_revision = backend_revision WHERE state = 'active'")
    op.create_check_constraint("ck_connector_provisioning_transition_phase", t, _PHASES)
    op.create_check_constraint(
        "ck_connector_provisioning_target_backend", t, "target_backend IS NULL OR target_backend IN ('native', 'n8n')")
    op.create_check_constraint(
        "ck_connector_provisioning_c4_revisions", t,
        "applied_backend_revision >= 0 AND template_revision >= 0 AND applied_template_revision >= 0 AND credential_revision > 0")
    op.create_table(
        "connector_rest_credentials",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("header_name", sa.String(128), nullable=False),
        sa.Column("encrypted_secret", sa.Text()),
        sa.Column("secret_fingerprint", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source_generation > 0 AND configuration_revision > 0", name="ck_connector_rest_credentials_fences"),
        sa.CheckConstraint("state IN ('ready', 'revoked')", name="ck_connector_rest_credentials_state"),
    )


def downgrade() -> None:
    """Drop the C4 additions."""
    op.drop_table("connector_rest_credentials")
    t = "connector_provisioning"
    for name in ("ck_connector_provisioning_c4_revisions", "ck_connector_provisioning_target_backend", "ck_connector_provisioning_transition_phase"):
        op.drop_constraint(name, t, type_="check")
    for column in ("credential_revision", "applied_template_revision", "template_revision", "old_workflow_id",
                   "transition_operation_id", "transition_phase", "target_backend", "applied_backend_revision"):
        op.drop_column(t, column)
