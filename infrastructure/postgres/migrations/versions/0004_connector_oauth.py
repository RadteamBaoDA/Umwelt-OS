"""Add source-bound OAuth attempts and token grants for read-only Google connectors."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_connector_oauth"
down_revision: str | Sequence[str] | None = "0003_model_test_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive, source-owned state for connector OAuth; existing auth rows are untouched."""
    op.execute("""CREATE TABLE connector_oauth_attempts (
    id UUID NOT NULL PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES owner(id) ON DELETE CASCADE,
    provider VARCHAR(64) NOT NULL,
    session_hash VARCHAR(64) NOT NULL,
    state_hash VARCHAR(64) NOT NULL UNIQUE,
    encrypted_verifier TEXT NOT NULL,
    source_generation INTEGER NOT NULL,
    configuration_revision INTEGER NOT NULL,
    credential_revision INTEGER NOT NULL,
    expected_token_revision INTEGER NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_connector_oauth_attempt_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive')),
    CONSTRAINT ck_connector_oauth_attempt_fences CHECK (source_generation > 0 AND configuration_revision >= 0 AND credential_revision >= 0 AND expected_token_revision >= 0)
)""")
    op.execute("CREATE INDEX ix_connector_oauth_attempt_expiry ON connector_oauth_attempts (expires_at)")
    op.execute("""CREATE TABLE connector_oauth_grants (
    source_id UUID NOT NULL PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES owner(id) ON DELETE CASCADE,
    provider VARCHAR(64) NOT NULL,
    operation_id UUID NOT NULL,
    source_generation INTEGER NOT NULL,
    configuration_revision INTEGER NOT NULL,
    credential_revision INTEGER NOT NULL,
    token_revision INTEGER NOT NULL,
    encrypted_tokens TEXT,
    granted_scopes JSONB NOT NULL,
    token_expires_at TIMESTAMP WITH TIME ZONE,
    state VARCHAR(32) NOT NULL,
    error_code VARCHAR(64),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT ck_connector_oauth_grant_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive')),
    CONSTRAINT ck_connector_oauth_grant_fences CHECK (source_generation > 0 AND configuration_revision > 0 AND credential_revision > 0 AND token_revision > 0),
    CONSTRAINT ck_connector_oauth_grant_state CHECK (state IN ('ready', 'revocation_pending', 'reconciliation_required', 'revoked'))
)""")


def downgrade() -> None:
    """Remove only the new connector OAuth state tables."""
    op.execute("DROP TABLE connector_oauth_grants")
    op.execute("DROP TABLE connector_oauth_attempts")
