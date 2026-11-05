"""Add ``workspace_storage_state``: the durable "originals erased" flag used by the S3 storage fence.

The local filesystem fence keeps this flag in a one-byte lock file. Object storage has no file locks, so
``PostgresStorageFence`` (``core.storage.pg_fence``) serializes writers with a per-workspace advisory
transaction lock and records permanent deletion here. The invariant is identical: once ``deleted_at`` is
set it is never cleared and the row is never removed, because delayed uploads may still hold a
pre-deletion SQL admission. A trigger enforces that at the database level. There is deliberately no
foreign key to ``workspaces``: the flag must outlive the workspace row.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_workspace_storage_state"
down_revision: str | Sequence[str] | None = "0015_worker_dead_letters"  # linear after the dead-letter revision merged first
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the flag table and the trigger that makes ``deleted_at`` write-once and rows permanent."""
    op.execute(
        "CREATE TABLE workspace_storage_state ("
        "workspace_id uuid PRIMARY KEY, "
        "deleted_at timestamptz)"
    )
    op.execute(
        "CREATE FUNCTION workspace_storage_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'workspace_storage_state rows are permanent'; "
        "END IF; "
        "IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN "
        "RAISE EXCEPTION 'workspace storage deletion flag cannot be undone or changed'; "
        "END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER workspace_storage_state_guard BEFORE UPDATE OR DELETE ON workspace_storage_state "
        "FOR EACH ROW EXECUTE FUNCTION workspace_storage_state_guard()"
    )


def downgrade() -> None:
    """Drop the trigger, function and table (discarding the erasure flags; downgrade is operator-only)."""
    op.execute("DROP TRIGGER workspace_storage_state_guard ON workspace_storage_state")
    op.execute("DROP FUNCTION workspace_storage_state_guard()")
    op.execute("DROP TABLE workspace_storage_state")
