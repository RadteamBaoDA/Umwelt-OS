"""Persist immutable specialist profile revisions and replay-safe run identity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p07_specialist_profiles"
down_revision: str | Sequence[str] | None = "p07_agent_chat_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add profile snapshots and retry identity after the accepted Chat lifecycle guard."""
    op.add_column("agent_runs", sa.Column("profile_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("agent_runs", sa.Column("profile_revision_hash", sa.String(length=64), nullable=True))
    op.add_column("agent_runs", sa.Column("client_request_id", sa.String(length=128), nullable=True))
    op.add_column("agent_runs", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_agent_runs_session_request",
        "agent_runs",
        ["owner_id", "auth_session_hash", "client_request_id"],
    )
    op.create_index("ix_agent_runs_owner_created", "agent_runs", ["owner_id", "created_at", "id"])
    op.create_table(
        "agent_profiles",
        sa.Column("profile_id", sa.String(length=24), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("model_alias", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_id = 1", name="ck_agent_profiles_single_owner"),
        sa.CheckConstraint(
            "profile_id IN ('supervisor','knowledge','research','personal','project','news','planning','automation')",
            name="ck_agent_profiles_id",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_agent_profiles_revision"),
        sa.CheckConstraint("octet_length(prompt) <= 32000", name="ck_agent_profiles_prompt_bytes"),
        sa.CheckConstraint("jsonb_array_length(allowed_tools) <= 32", name="ck_agent_profiles_tools_count"),
        sa.CheckConstraint("jsonb_array_length(source_ids) <= 32", name="ck_agent_profiles_sources_count"),
    )
    op.create_table(
        "agent_profile_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", sa.String(length=24), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_id = 1", name="ck_agent_profile_revisions_single_owner"),
        sa.CheckConstraint("revision >= 1", name="ck_agent_profile_revisions_revision"),
        sa.UniqueConstraint("profile_id", "revision", name="uq_agent_profile_revisions_version"),
    )


def downgrade() -> None:
    """Remove only additive specialist storage, retaining the accepted parent migration unchanged."""
    op.drop_table("agent_profile_revisions")
    op.drop_table("agent_profiles")
    op.drop_index("ix_agent_runs_owner_created", table_name="agent_runs")
    op.drop_constraint("uq_agent_runs_session_request", "agent_runs", type_="unique")
    op.drop_column("agent_runs", "request_hash")
    op.drop_column("agent_runs", "client_request_id")
    op.drop_column("agent_runs", "profile_revision_hash")
    op.drop_column("agent_runs", "profile_snapshot")
