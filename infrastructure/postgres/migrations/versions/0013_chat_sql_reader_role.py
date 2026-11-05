"""Create the least-privilege ``umwelt_chat_sql_reader`` role used by the chat SQL tool.

``query_collected_data`` (mode ``sql``) executes model-written SELECTs. Besides the parser allowlist,
the executor runs them after ``SET LOCAL ROLE umwelt_chat_sql_reader`` in a read-only transaction, so a
validator bypass still cannot read ``chat_messages``, ``memories``, ``ai_settings``, credentials, etc.
The role is NOLOGIN/NOSUPERUSER/NOINHERIT and is granted no table, sequence or function privileges;
the application grants it SELECT on two per-query TEMP views only. It is granted to ``CURRENT_USER``
(the migration owner, i.e. the application's database user) so that user can ``SET ROLE`` to it.
Role creation is guarded because roles are cluster-wide and may already exist.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_chat_sql_reader_role"
down_revision: str | Sequence[str] | None = "0012_merge_live_and_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "umwelt_chat_sql_reader"


def upgrade() -> None:
    """Create the NOLOGIN reader role if missing and allow the current user to assume it."""
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _ROLE + "') THEN "  # noqa: S608  # _ROLE is a module constant, not input
        "CREATE ROLE " + _ROLE + " NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS; END IF; END $$"
    )
    # Defensive: a pre-existing role may have been created with broader attributes.
    op.execute(
        f"ALTER ROLE {_ROLE} NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    )
    # Direct grants to the role stay empty (grants inherited from PUBLIC cannot be revoked per role).
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_ROLE}")
    op.execute("DO $$ BEGIN EXECUTE format('REVOKE ALL ON DATABASE %I FROM " + _ROLE + "', current_database()); END $$")
    op.execute(f"GRANT {_ROLE} TO CURRENT_USER")


def downgrade() -> None:
    """Drop the reader role (it owns nothing and holds no durable privileges, so no data is affected)."""
    op.execute(f"DROP ROLE IF EXISTS {_ROLE}")
