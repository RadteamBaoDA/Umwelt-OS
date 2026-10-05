"""Persist whether an agent run originally required a live Chat conversation."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "p07_agent_chat_lifecycle"
down_revision: str | Sequence[str] | None = "p07_approvals_effects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain Chat link requirements after draining ambiguous live legacy runs.

    Reject uncancelled runnable legacy rows with no surviving link because their original
    optional conversation association cannot be reconstructed safely.
    """
    # Exclude old API/worker writers and Chat cascades across precondition and backfill.
    op.execute(sa.text(
        "LOCK TABLE agent_runs, chat_agent_activity_links IN SHARE ROW EXCLUSIVE MODE"
    ))
    op.execute(sa.text(
        "DO $bbd_chat_lifecycle$ "
        "BEGIN "
        "IF EXISTS ("
        "SELECT 1 FROM agent_runs AS r "
        "WHERE r.status IN ('queued', 'running', 'waiting_approval') "
        "AND r.cancel_requested = false "
        "AND NOT EXISTS ("
        "SELECT 1 FROM chat_agent_activity_links AS l WHERE l.agent_run_id = r.id"
        ")"
        ") THEN "
        "RAISE EXCEPTION 'p07_agent_chat_lifecycle requires draining legacy active unlinked agent runs before activation'; "
        "END IF; "
        "END; "
        "$bbd_chat_lifecycle$;"
    ))
    op.add_column(
        "agent_runs",
        sa.Column("chat_link_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Existing live links are authoritative; deleted links cannot be reconstructed, so P07 must
    # apply this additive revision before activating run creation or Chat deletion workflows.
    op.execute(sa.text(
        "UPDATE agent_runs SET chat_link_required = true "
        "WHERE EXISTS (SELECT 1 FROM chat_agent_activity_links "
        "WHERE chat_agent_activity_links.agent_run_id = agent_runs.id)"
    ))


def downgrade() -> None:
    """Remove only the additive lifecycle marker while preserving the parent revision schema."""
    op.drop_column("agent_runs", "chat_link_required")
