"""Create owner-scoped bounded agent runs and the pinned LangGraph saver schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p07_agent_runs"
down_revision: str | Sequence[str] | None = "p07_mcp_stdio_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable run/claim state and exact checkpoint-postgres 3.1.2 tables/indexes."""
    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "agent_runs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_session_hash", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(40), nullable=False),
        sa.Column("workflow_version", sa.String(40), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("checkpoint_schema_version", sa.Integer(), nullable=False),
        sa.Column("checkpoint_thread_id", sa.String(36), nullable=False, unique=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("allowed_tools", jsonb_type, nullable=False),
        sa.Column("tool_contracts", jsonb_type, nullable=False),
        sa.Column("source_fences", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_started_at", timestamp),
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_usage", sa.Integer()),
        sa.Column("token_usage_unknown", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("token_budget", sa.Integer()),
        sa.CheckConstraint("token_budget IS NULL OR token_budget BETWEEN 1 AND 2000000", name="ck_agent_runs_token_budget"),
        sa.Column("answer", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("activities", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", timestamp),
        sa.CheckConstraint("owner_id = 1", name="ck_agent_runs_single_owner"),
        sa.CheckConstraint("agent_id = 'assistant'", name="ck_agent_runs_known_workflow"),
        sa.CheckConstraint("status IN ('queued','running','waiting_approval','succeeded','failed','cancelled')", name="ck_agent_runs_status"),
        sa.CheckConstraint("steps BETWEEN 0 AND 20", name="ck_agent_runs_steps"),
        sa.CheckConstraint("tool_calls BETWEEN 0 AND 10", name="ck_agent_runs_tool_calls"),
        sa.CheckConstraint("active_seconds BETWEEN 0 AND 300", name="ck_agent_runs_active_seconds"),
        sa.CheckConstraint("dispatch_generation >= 1 AND claim_generation >= 0", name="ck_agent_runs_generations"),
        sa.CheckConstraint("checkpoint_schema_version = 1", name="ck_agent_runs_checkpoint_version"),
        sa.CheckConstraint("length(workflow_version) BETWEEN 1 AND 40 AND length(prompt_version) BETWEEN 1 AND 40", name="ck_agent_runs_version_lengths"),
        sa.CheckConstraint("length(auth_session_hash) = 64", name="ck_agent_runs_auth_hash"),
        sa.CheckConstraint("octet_length(prompt) <= 32000", name="ck_agent_runs_prompt_size"),
        sa.CheckConstraint("jsonb_array_length(allowed_tools) <= 200", name="ck_agent_runs_tool_bound"),
        sa.CheckConstraint("octet_length(tool_contracts::text) <= 64000", name="ck_agent_runs_contract_bytes"),
        sa.CheckConstraint("octet_length(source_fences::text) <= 512000", name="ck_agent_runs_source_fence_bytes"),
        sa.CheckConstraint("jsonb_array_length(activities) <= 64", name="ck_agent_runs_activity_bound"),
    )
    op.create_index("ix_agent_runs_dispatch", "agent_runs", ["status", "updated_at"])
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("run_id", uuid_type, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("tool_version", sa.String(40)),
        sa.Column("schema_fingerprint", sa.String(64)),
        sa.Column("arguments", jsonb_type, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32)),
        sa.Column("evidence_refs", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", timestamp),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_agent_tool_calls_ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_tool_calls_ordinal"),
        sa.CheckConstraint("status IN ('started','succeeded','denied','failed')", name="ck_agent_tool_calls_status"),
        sa.CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_tool_calls_argument_bytes"),
        sa.CheckConstraint("jsonb_array_length(evidence_refs) <= 100", name="ck_agent_tool_calls_evidence_bound"),
    )
    op.create_index("ix_agent_tool_calls_run", "agent_tool_calls", ["run_id", "ordinal"])

    op.create_table(
        "chat_agent_activity_links",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("conversation_id", uuid_type, sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_run_id", uuid_type, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("auth_session_hash", sa.String(64), nullable=False),
        sa.Column("activities", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("ephemeral", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("agent_run_id", name="uq_chat_agent_activity_run"),
        sa.CheckConstraint("owner_id = 1 AND length(auth_session_hash) = 64", name="ck_chat_agent_activity_owner_auth"),
        sa.CheckConstraint("jsonb_array_length(activities) <= 64", name="ck_chat_agent_activity_bound"),
    )
    op.create_index("ix_chat_agent_activity_conversation", "chat_agent_activity_links", ["conversation_id", "updated_at"])

    # These SQL definitions match checkpoint-postgres 3.1.2 migration versions 0–8.
    # The release uses CREATE INDEX CONCURRENTLY, which cannot run in Alembic's transaction;
    # ordinary indexes are equivalent for these new empty tables during deployment.
    op.create_table("checkpoint_migrations", sa.Column("v", sa.Integer(), primary_key=True))
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text()),
        sa.Column("type", sa.Text()),
        sa.Column("checkpoint", jsonb_type, nullable=False),
        sa.Column("metadata", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", sa.LargeBinary()),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "channel", "version"),
    )
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("task_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text()),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )
    op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"])
    op.create_index("checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"])
    op.create_index("checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"])
    op.bulk_insert(
        sa.table("checkpoint_migrations", sa.column("v", sa.Integer())),
        [{"v": version} for version in range(9)],
    )
    op.execute(
        """
        CREATE FUNCTION purge_agent_checkpoints() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM checkpoint_writes WHERE thread_id = OLD.checkpoint_thread_id;
            DELETE FROM checkpoint_blobs WHERE thread_id = OLD.checkpoint_thread_id;
            DELETE FROM checkpoints WHERE thread_id = OLD.checkpoint_thread_id;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_agent_runs_purge_checkpoints BEFORE DELETE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION purge_agent_checkpoints()"
    )


def downgrade() -> None:
    """Remove this task's checkpoint trigger/schema and owning run table in dependency order."""
    op.execute("DROP TRIGGER trg_agent_runs_purge_checkpoints ON agent_runs")
    op.execute("DROP FUNCTION purge_agent_checkpoints()")
    op.drop_index("ix_chat_agent_activity_conversation", table_name="chat_agent_activity_links")
    op.drop_table("chat_agent_activity_links")
    op.drop_index("checkpoint_writes_thread_id_idx", table_name="checkpoint_writes")
    op.drop_index("checkpoint_blobs_thread_id_idx", table_name="checkpoint_blobs")
    op.drop_index("checkpoints_thread_id_idx", table_name="checkpoints")
    op.drop_table("checkpoint_writes")
    op.drop_table("checkpoint_blobs")
    op.drop_table("checkpoints")
    op.drop_table("checkpoint_migrations")
    op.drop_index("ix_agent_tool_calls_run", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")
    op.drop_index("ix_agent_runs_dispatch", table_name="agent_runs")
    op.drop_table("agent_runs")
