"""Create ``worker_dead_letters``: durable record of arq jobs that exhausted their retries.

The table is infrastructure bookkeeping (no workspace or user data): job identity, a SHA-256 digest of
the arguments, identifier-only replay arguments when safe, a scrubbed truncated error and attempt /
failure timestamps. ``(queue, job_id)`` is unique so a replayed job that fails again updates its row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_worker_dead_letters"
down_revision: str | Sequence[str] | None = "0014_perf_indexes_and_safety"  # linear after the perf-index revision merged first
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the dead-letter table and its recency index."""
    op.create_table(
        "worker_dead_letters",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("queue", sa.String(64), nullable=False),
        sa.Column("job_name", sa.String(128), nullable=False),
        sa.Column("job_id", sa.String(256), nullable=False),
        sa.Column("args_digest", sa.String(64), nullable=False),
        sa.Column("args_ref", postgresql.JSONB(), nullable=True),
        sa.Column("replayable", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_class", sa.String(128), nullable=False),
        sa.Column("error_message", sa.String(300), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("queue", "job_id", name="uq_worker_dead_letters_queue_job"),
    )
    op.create_index("ix_worker_dead_letters_last_failed", "worker_dead_letters", ["last_failed_at"])


def downgrade() -> None:
    """Drop the dead-letter table (its rows are operational history only)."""
    op.drop_index("ix_worker_dead_letters_last_failed", table_name="worker_dead_letters")
    op.drop_table("worker_dead_letters")
