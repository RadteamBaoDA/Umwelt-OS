"""Drop the in-app backup barrier tables; backups are delegated to the cloud provider.

The P12 write-admission barrier (control singleton, operation receipts, activity leases) was removed
from the application together with the backup module. Operators now snapshot the Postgres, app-data,
n8n-data and graph-data volumes through their provider. Foreign keys, check constraints and the
activity index are dropped with their tables; no enums, triggers or functions were created for them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "r21_drop_backup_control"
down_revision: str | Sequence[str] | None = "r20_search_tsv_and_trgm_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop activity, control (FK to operations) and operations, in dependency order."""
    op.execute("DROP INDEX IF EXISTS ix_p12_backup_activity_active_epoch")
    op.execute("DROP TABLE IF EXISTS p12_backup_activity")
    op.execute("DROP TABLE IF EXISTS p12_backup_control")
    op.execute("DROP TABLE IF EXISTS p12_backup_operations")


def downgrade() -> None:
    """Irreversible: the backup feature no longer exists in the application, so the tables are not recreated."""
    raise RuntimeError("r21_drop_backup_control is irreversible; the removed backup tables are not recreated")
