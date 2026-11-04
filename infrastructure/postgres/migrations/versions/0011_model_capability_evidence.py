"""Make ``model_test_results`` the single persistent model capability evidence store.

Adds ``capability`` (primary-key member; existing rows are the chat/Test-button summaries) and
``model_identity`` (NULL for legacy rows, which stay display-only and never gate a request). Evidence no
longer lives in Redis with a 24h TTL; the model identity invalidates it when credentials/endpoint/defaults change.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_model_capability_evidence"
down_revision: str | Sequence[str] | None = "0010_observations_vn_stock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the capability dimension to the primary key and the identity column."""
    op.execute("ALTER TABLE model_test_results ADD COLUMN capability VARCHAR(16) NOT NULL DEFAULT 'chat'")
    op.execute("ALTER TABLE model_test_results ADD COLUMN model_identity VARCHAR(64)")
    op.execute("ALTER TABLE model_test_results DROP CONSTRAINT model_test_results_pkey")
    op.execute("ALTER TABLE model_test_results ADD PRIMARY KEY (workspace_id, provider_id, model_id, capability)")
    op.execute(
        "ALTER TABLE model_test_results ADD CONSTRAINT ck_model_test_results_capability "
        "CHECK (capability IN ('chat', 'streaming', 'embeddings', 'structured', 'tools', 'reranking'))")


def downgrade() -> None:
    """Keep one row per model (the chat summary wins), then restore the original key and drop the columns."""
    op.execute("DELETE FROM model_test_results WHERE capability <> 'chat'")
    op.execute("ALTER TABLE model_test_results DROP CONSTRAINT ck_model_test_results_capability")
    op.execute("ALTER TABLE model_test_results DROP CONSTRAINT model_test_results_pkey")
    op.execute("ALTER TABLE model_test_results ADD PRIMARY KEY (workspace_id, provider_id, model_id)")
    op.execute("ALTER TABLE model_test_results DROP COLUMN model_identity")
    op.execute("ALTER TABLE model_test_results DROP COLUMN capability")
