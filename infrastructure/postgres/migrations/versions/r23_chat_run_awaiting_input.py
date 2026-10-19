"""Add the ``awaiting_input`` chat run status and the pending user-input request column.

A run parked on a ``request_user_input`` tool call keeps ONE open request in ``input_request`` (JSONB:
request_id, kind, prompt, options/slots, expires_at, state, answer). A column rather than a table: at most one
request is open per run (the worker blocks on it), the request dies with the run, and stream events already
hold the history. The old/new status CHECK is replaced in one transaction.

Chained after r22_chat_todos (r21 -> r22 -> r23 -> r24).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r23_chat_run_awaiting_input"
down_revision: str | Sequence[str] | None = "r22_chat_todos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('pending', 'streaming', 'completed', 'cancelled', 'failed')"
_NEW = "status IN ('pending', 'streaming', 'awaiting_input', 'completed', 'cancelled', 'failed')"


def upgrade() -> None:
    """Widen the run status CHECK with ``awaiting_input`` and add the nullable ``input_request`` JSONB."""
    op.add_column("chat_response_runs", sa.Column("input_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_constraint("ck_chat_response_runs_status", "chat_response_runs", type_="check")
    op.create_check_constraint("ck_chat_response_runs_status", "chat_response_runs", _NEW)


def downgrade() -> None:
    """Fail parked runs, then restore the previous CHECK and drop the request column."""
    op.execute("UPDATE chat_response_runs SET status = 'failed', completed_at = now() WHERE status = 'awaiting_input'")
    op.drop_constraint("ck_chat_response_runs_status", "chat_response_runs", type_="check")
    op.create_check_constraint("ck_chat_response_runs_status", "chat_response_runs", _OLD)
    op.drop_column("chat_response_runs", "input_request")
