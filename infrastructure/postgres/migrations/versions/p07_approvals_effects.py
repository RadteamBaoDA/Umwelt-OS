"""Add immutable tool approvals and independent no-replay effect tombstones."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p07_approvals_effects"
down_revision: str | Sequence[str] | None = "p07_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive owner decision/effect records while retaining old run checkpoints."""
    uuid_type = postgresql.UUID(as_uuid=True)
    timestamp = sa.DateTime(timezone=True)
    op.drop_constraint("ck_agent_tool_calls_status", "agent_tool_calls", type_="check")
    op.create_check_constraint(
        "ck_agent_tool_calls_status", "agent_tool_calls",
        "status IN ('started','approval_pending','succeeded','denied','failed')",
    )
    op.create_table(
        "agent_approvals",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("action_id", uuid_type, nullable=False, unique=True),
        sa.Column("run_id", uuid_type, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("auth_session_hash", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("tool_version", sa.String(40), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("destination_id", sa.String(200), nullable=False),
        sa.Column("destination_revision", sa.String(64), nullable=False),
        sa.Column("source_fences", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("resolved_at", timestamp),
        sa.Column("updated_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_agent_approvals_run_ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_approvals_ordinal"),
        sa.CheckConstraint("owner_id = 1", name="ck_agent_approvals_single_owner"),
        sa.CheckConstraint("status IN ('pending','approved','denied','expired','cancelled','requires_review')", name="ck_agent_approvals_status"),
        sa.CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_approvals_argument_bytes"),
    )
    op.create_index("ix_agent_approvals_owner_state_expiry", "agent_approvals", ["owner_id", "status", "expires_at"])
    op.create_table(
        "agent_effects",
        sa.Column("action_id", uuid_type, primary_key=True),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False, unique=True),
        sa.Column("profile_alias", sa.String(40), nullable=False),
        sa.Column("profile_revision", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("result_status_code", sa.Integer()),
        sa.Column("result_reference", sa.String(256)),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('reserved','in_flight','succeeded','failed','requires_review')", name="ck_agent_effects_state"),
        sa.CheckConstraint("payload IS NULL OR octet_length(payload::text) <= 64000", name="ck_agent_effects_payload_bytes"),
    )
    op.create_index("ix_agent_effects_run_created", "agent_effects", ["run_id", "created_at"])
    # Prevent a later code path from rewriting what the owner approved or reusing an action key.
    op.execute(sa.text("""
        CREATE FUNCTION protect_agent_approval_identity() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.id, NEW.action_id, NEW.run_id, NEW.owner_id, NEW.auth_session_hash,
                   NEW.ordinal, NEW.tool_name, NEW.tool_version, NEW.schema_fingerprint,
                   NEW.argument_hash, NEW.destination_id, NEW.destination_revision,
                   NEW.created_at, NEW.expires_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.action_id, OLD.run_id, OLD.owner_id, OLD.auth_session_hash,
                   OLD.ordinal, OLD.tool_name, OLD.tool_version, OLD.schema_fingerprint,
                   OLD.argument_hash, OLD.destination_id, OLD.destination_revision,
                   OLD.created_at, OLD.expires_at)
               OR (OLD.arguments IS DISTINCT FROM NEW.arguments AND NEW.arguments IS NOT NULL)
               OR (OLD.source_fences IS DISTINCT FROM NEW.source_fences AND NEW.source_fences <> '{}'::jsonb) THEN
                RAISE EXCEPTION 'agent approval identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_agent_approval_identity_immutable
        BEFORE UPDATE ON agent_approvals
        FOR EACH ROW EXECUTE FUNCTION protect_agent_approval_identity()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION protect_agent_effect_tombstone() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.action_id, NEW.run_id, NEW.provider_key, NEW.profile_alias,
                   NEW.profile_revision, NEW.payload_hash, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.action_id, OLD.run_id, OLD.provider_key, OLD.profile_alias,
                   OLD.profile_revision, OLD.payload_hash, OLD.created_at)
               OR (OLD.payload IS DISTINCT FROM NEW.payload AND NEW.payload IS NOT NULL) THEN
                RAISE EXCEPTION 'agent effect identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_agent_effect_tombstone_immutable
        BEFORE UPDATE ON agent_effects
        FOR EACH ROW EXECUTE FUNCTION protect_agent_effect_tombstone()
    """))


def downgrade() -> None:
    """Remove unshipped approval/effect tables and restore the prior tool-call status domain."""
    op.execute(sa.text("DROP TRIGGER trg_agent_effect_tombstone_immutable ON agent_effects"))
    op.execute(sa.text("DROP FUNCTION protect_agent_effect_tombstone()"))
    op.execute(sa.text("DROP TRIGGER trg_agent_approval_identity_immutable ON agent_approvals"))
    op.execute(sa.text("DROP FUNCTION protect_agent_approval_identity()"))
    op.drop_index("ix_agent_effects_run_created", table_name="agent_effects")
    op.drop_table("agent_effects")
    op.drop_index("ix_agent_approvals_owner_state_expiry", table_name="agent_approvals")
    op.drop_table("agent_approvals")
    op.drop_constraint("ck_agent_tool_calls_status", "agent_tool_calls", type_="check")
    op.create_check_constraint(
        "ck_agent_tool_calls_status", "agent_tool_calls",
        "status IN ('started','succeeded','denied','failed')",
    )
