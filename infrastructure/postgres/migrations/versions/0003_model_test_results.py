"""Add ``model_test_results``: last Test-button outcome per provider model (display only)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_model_test_results"
down_revision: str | Sequence[str] | None = "0002_observations_provider_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table; one row per (workspace, provider, model), upserted by each test."""
    op.execute("""CREATE TABLE model_test_results (
    workspace_id UUID NOT NULL,
    provider_id VARCHAR(64) NOT NULL,
    model_id VARCHAR(256) NOT NULL,
    result VARCHAR(16) NOT NULL,
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (workspace_id, provider_id, model_id),
    CONSTRAINT fk_model_test_results_workspace FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT,
    CONSTRAINT ck_model_test_results_result CHECK (result IN ('supported', 'failed', 'unsupported'))
)""")


def downgrade() -> None:
    """Drop the table; only cached display state is lost."""
    op.execute("DROP TABLE model_test_results")
