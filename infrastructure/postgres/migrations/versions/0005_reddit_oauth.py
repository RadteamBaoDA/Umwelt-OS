"""Allow Reddit on the source-bound connector OAuth records."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_reddit_oauth"
down_revision: str | Sequence[str] | None = "0004_connector_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen only the provider checks; existing OAuth rows and credentials remain untouched."""
    op.execute("ALTER TABLE connector_oauth_attempts DROP CONSTRAINT ck_connector_oauth_attempt_provider")
    op.execute("ALTER TABLE connector_oauth_attempts ADD CONSTRAINT ck_connector_oauth_attempt_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive', 'reddit'))")
    op.execute("ALTER TABLE connector_oauth_grants DROP CONSTRAINT ck_connector_oauth_grant_provider")
    op.execute("ALTER TABLE connector_oauth_grants ADD CONSTRAINT ck_connector_oauth_grant_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive', 'reddit'))")


def downgrade() -> None:
    """Restore the original provider checks after Reddit grants have been disconnected or removed."""
    op.execute("ALTER TABLE connector_oauth_attempts DROP CONSTRAINT ck_connector_oauth_attempt_provider")
    op.execute("ALTER TABLE connector_oauth_attempts ADD CONSTRAINT ck_connector_oauth_attempt_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive'))")
    op.execute("ALTER TABLE connector_oauth_grants DROP CONSTRAINT ck_connector_oauth_grant_provider")
    op.execute("ALTER TABLE connector_oauth_grants ADD CONSTRAINT ck_connector_oauth_grant_provider CHECK (provider IN ('google_mail', 'google_calendar', 'google_drive'))")
