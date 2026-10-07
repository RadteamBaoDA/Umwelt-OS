"""Introduce invite-only account/default-workspace identity without enabling new sessions.

Preserve legacy credentials, Google issuer/subject keys, sessions and all domain IDs. Empty
databases remain unconfigured; authenticated setup creates the bootstrap identity atomically.
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_identity"
down_revision: str | Sequence[str] | None = "p12_evidence_version_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add identity constraints and backfill only the actual bootstrap owner in one migration.

    The default pointer starts nullable to resolve circular ownership without temporary
    invalid credentials. Match it to the workspace owner through a composite FK. Repair
    sequence allocation above existing IDs while preserving any higher sequence watermark.
    No session invalidation occurs; legacy admission remains bootstrap-only in application code.
    """
    op.drop_constraint("ck_owner_singleton", "owner", type_="check")
    op.add_column("owner", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("owner", sa.Column("account_state", sa.String(16), server_default="active", nullable=False))
    op.add_column("owner", sa.Column("default_workspace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("owner", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("owner", sa.Column("email_verification_source", sa.String(32), nullable=True))
    op.create_check_constraint("ck_owner_positive_id", "owner", "id > 0")
    op.create_check_constraint("ck_owner_account_state", "owner", "account_state IN ('active', 'disabled')")
    op.create_check_constraint("ck_owner_normalized_email", "owner", "email IS NULL OR (email = lower(btrim(email)) AND length(email) > 0)")
    op.create_check_constraint(
        "ck_owner_email_verification", "owner",
        "(email_verified_at IS NULL AND email_verification_source IS NULL) OR "
        "(email IS NOT NULL AND email_verified_at IS NOT NULL AND email_verification_source IS NOT NULL "
        "AND email_verification_source = 'google_oidc')",
    )
    op.create_index("uq_owner_email", "owner", ["email"], unique=True)
    # Explicit bootstrap ID 1 never advances a serial sequence. Do not let later implicit
    # account allocation collide, including for an empty DB where setup is still pending.
    op.execute(sa.text("""
        DO $$
        DECLARE account_sequence text; sequence_watermark bigint; account_watermark bigint;
        BEGIN
            account_sequence := pg_get_serial_sequence('owner', 'id');
            IF account_sequence IS NULL THEN
                CREATE SEQUENCE owner_id_seq OWNED BY owner.id;
                ALTER TABLE owner ALTER COLUMN id SET DEFAULT nextval('owner_id_seq'::regclass);
                account_sequence := 'owner_id_seq';
            END IF;
            EXECUTE format('SELECT last_value FROM %s', account_sequence) INTO sequence_watermark;
            SELECT COALESCE(MAX(id), 0) INTO account_watermark FROM owner;
            PERFORM setval(account_sequence::regclass, GREATEST(account_watermark, sequence_watermark, 1), true);
        END $$
    """))
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["owner.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user_id"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_workspaces_id_owner"),
        sa.CheckConstraint("is_default", name="ck_workspaces_default_only"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_workspaces_positive_revision"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name"),
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["owner.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_memberships_matching_owner", ondelete="CASCADE",
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_workspace_memberships_role"),
        sa.CheckConstraint("revision > 0", name="ck_workspace_memberships_positive_revision"),
        sa.CheckConstraint(
            "(role = 'owner' AND owner_user_id IS NOT NULL AND owner_user_id = user_id) OR "
            "(role = 'member' AND owner_user_id IS NULL)", name="ck_workspace_memberships_owner_marker",
        ),
    )
    op.create_index("uq_workspace_memberships_owner", "workspace_memberships", ["workspace_id"], unique=True, postgresql_where=sa.text("role = 'owner'"))
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])
    op.create_table(
        "workspace_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["owner.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "invited_by_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_invitations_matching_owner", ondelete="CASCADE",
        ),
        sa.CheckConstraint("email = lower(btrim(email)) AND length(email) > 0", name="ck_workspace_invitations_normalized_email"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_workspace_invitations_token_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_workspace_invitations_expiry"),
        sa.CheckConstraint(
            "(accepted_at IS NULL AND accepted_by_user_id IS NULL) OR "
            "(accepted_at IS NOT NULL AND accepted_by_user_id IS NOT NULL AND revoked_at IS NULL)",
            name="ck_workspace_invitations_acceptance",
        ),
    )
    op.create_index("uq_workspace_invitations_token_hash", "workspace_invitations", ["token_hash"], unique=True)
    op.create_index("ix_workspace_invitations_workspace_id", "workspace_invitations", ["workspace_id"])
    op.create_foreign_key(
        "fk_owner_owned_default_workspace", "owner", "workspaces",
        ["default_workspace_id", "id"], ["id", "owner_user_id"], ondelete="RESTRICT",
    )
    # A new UUID belongs only to the proven legacy owner; no credentials/data IDs change.
    # Literal UUID generation requires no pgcrypto extension and also renders in offline SQL.
    legacy_workspace_id = str(uuid4())
    op.execute(sa.text(
        f"INSERT INTO workspaces (id, name, owner_user_id) "
        f"SELECT '{legacy_workspace_id}'::uuid, 'Private workspace', id FROM owner WHERE id = 1"
    ))
    op.execute(sa.text(
        "INSERT INTO workspace_memberships (workspace_id, user_id, role, owner_user_id) "
        "SELECT id, owner_user_id, 'owner', owner_user_id FROM workspaces WHERE owner_user_id = 1"
    ))
    op.execute(sa.text(
        "UPDATE owner SET default_workspace_id = w.id FROM workspaces w "
        "WHERE owner.id = 1 AND w.owner_user_id = owner.id"
    ))


def downgrade() -> None:
    """Reject irreversible identity loss before restoring legacy singleton constraints.

    Extra accounts, foreign workspaces/memberships, any invitation, new email/provenance or
    disabled state cannot fit the old schema. Raise with table/count evidence instead of
    silently deleting it. Later W2 migrations must first undo/guard their own scoped data.
    """
    # Run guards in SQL for both online migrations and generated downgrade scripts.
    op.execute(sa.text("""
        DO $$
        DECLARE account_count bigint; workspace_count bigint; membership_count bigint;
                invitation_count bigint; incompatible_accounts bigint;
                incompatible_workspaces bigint; incompatible_memberships bigint;
        BEGIN
            SELECT count(*), count(*) FILTER (WHERE id <> 1 OR account_state <> 'active'
                OR email IS NOT NULL OR email_verified_at IS NOT NULL OR email_verification_source IS NOT NULL
                OR default_workspace_id IS NULL)
                INTO account_count, incompatible_accounts FROM owner;
            SELECT count(*), count(*) FILTER (WHERE owner_user_id <> 1 OR NOT is_default
                OR configuration_revision <> 1 OR name <> 'Private workspace')
                INTO workspace_count, incompatible_workspaces FROM workspaces;
            SELECT count(*), count(*) FILTER (WHERE user_id <> 1 OR role <> 'owner'
                OR owner_user_id IS DISTINCT FROM 1 OR revision <> 1)
                INTO membership_count, incompatible_memberships FROM workspace_memberships;
            SELECT count(*) INTO invitation_count FROM workspace_invitations;
            IF account_count > 1 OR workspace_count <> account_count OR membership_count <> account_count
                OR invitation_count <> 0 OR incompatible_accounts <> 0
                OR incompatible_workspaces <> 0 OR incompatible_memberships <> 0 THEN
                RAISE EXCEPTION 'p14_identity downgrade refused: owner=%, workspaces=%, workspace_memberships=%, workspace_invitations=%, incompatible owner/workspace/membership=%/%/%',
                    account_count, workspace_count, membership_count, invitation_count,
                    incompatible_accounts, incompatible_workspaces, incompatible_memberships;
            END IF;
        END $$
    """))
    op.drop_constraint("fk_owner_owned_default_workspace", "owner", type_="foreignkey")
    op.drop_table("workspace_invitations")
    op.drop_table("workspace_memberships")
    op.drop_table("workspaces")
    op.drop_index("uq_owner_email", table_name="owner")
    for constraint in ("ck_owner_email_verification", "ck_owner_normalized_email", "ck_owner_account_state", "ck_owner_positive_id"):
        op.drop_constraint(constraint, "owner", type_="check")
    for column in ("email_verification_source", "email_verified_at", "default_workspace_id", "account_state", "email"):
        op.drop_column("owner", column)
    op.create_check_constraint("ck_owner_singleton", "owner", "id = 1")
