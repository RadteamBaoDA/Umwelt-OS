"""Create the ``umwelt_chat_sql_login`` role and permanent fenced views for the chat SQL tool.

``SET ROLE umwelt_chat_sql_reader`` (0013) is authorised against ``session_user``, so SQL that escaped the
parser allowlist could ``RESET ROLE`` back to the application user. The chat SQL tool therefore connects
directly as ``umwelt_chat_sql_login``: a LOGIN role that is a member of no role (in particular not of the
application role), has no table privileges, defaults to read-only transactions, and carries finite
``statement_timeout`` / ``idle_in_transaction_session_timeout`` role settings.

The role can only read two views in the ``chat_sql`` schema (owned by the migration user, so view bodies run
with the owner's rights): ``scoped_observations`` and ``scoped_documents``. The workspace and allowed-source
ids are passed per transaction through the ``umwelt.chat_sql_workspace`` / ``umwelt.chat_sql_sources`` settings
(``set_config(..., true)``); unset settings yield zero rows. Those settings are user-changeable, so the
fence is enforced by the parser allowlist (which rejects ``set_config``); the database layer guarantees
read-only access to these two views and nothing else.

No password is stored here: the role has none until ``python -m core.chat_sql_provision`` applies the one
from ``CHAT_SQL_READER_DATABASE_URL`` (a role without a password cannot authenticate over scram/md5).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_chat_sql_login_role"
down_revision: str | Sequence[str] | None = "0016_workspace_storage_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "umwelt_chat_sql_login"
_SCHEMA = "chat_sql"

_OBSERVATIONS_VIEW = """
CREATE OR REPLACE VIEW chat_sql.scoped_observations WITH (security_barrier = true) AS
SELECT o.id, o.source_id, o.provider, o.metric, o.symbol, o.region, o.value, o.unit, o.currency,
       o.observed_at, o.published_at, o.quality, o.is_current, o.document_id, o.document_version_id
FROM public.observations o
WHERE o.workspace_id = NULLIF(current_setting('umwelt.chat_sql_workspace', true), '')::uuid AND o.source_id = ANY(string_to_array(NULLIF(current_setting('umwelt.chat_sql_sources', true), ''), ',')::uuid[])
"""
_DOCUMENTS_VIEW = """
CREATE OR REPLACE VIEW chat_sql.scoped_documents WITH (security_barrier = true) AS
SELECT d.id, d.source_id, s.provider, d.title, d.canonical_url, d.published_at, d.observed_at,
       d.language, LEFT(dv.content, 600) AS content_excerpt, dv.id AS document_version_id
FROM public.documents d
JOIN public.sources s ON s.id = d.source_id AND s.workspace_id = d.workspace_id
JOIN public.document_versions dv ON dv.document_id = d.id AND dv.version_number = d.current_version
WHERE d.workspace_id = NULLIF(current_setting('umwelt.chat_sql_workspace', true), '')::uuid AND d.source_id = ANY(string_to_array(NULLIF(current_setting('umwelt.chat_sql_sources', true), ''), ',')::uuid[])
"""


def upgrade() -> None:
    """Create the passwordless LOGIN role, the fenced views and the minimal grants (idempotent)."""
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _ROLE + "') THEN "  # noqa: S608  # module constant
        "CREATE ROLE " + _ROLE + " LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8; END IF; END $$"
    )
    # Defensive: a pre-existing role may carry broader attributes.
    op.execute(
        f"ALTER ROLE {_ROLE} LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION "
        "NOBYPASSRLS CONNECTION LIMIT 8"
    )
    op.execute(f"ALTER ROLE {_ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {_ROLE} SET statement_timeout = '2s'")
    op.execute(f"ALTER ROLE {_ROLE} SET idle_in_transaction_session_timeout = '10s'")
    op.execute(f"ALTER ROLE {_ROLE} SET search_path = {_SCHEMA}, pg_catalog")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
    op.execute(_OBSERVATIONS_VIEW)
    op.execute(_DOCUMENTS_VIEW)
    op.execute(f"REVOKE ALL ON SCHEMA {_SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {_SCHEMA} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_ROLE}")
    op.execute(f"GRANT SELECT ON chat_sql.scoped_observations, chat_sql.scoped_documents TO {_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_ROLE}")
    op.execute("DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO " + _ROLE + "', current_database()); END $$")


def downgrade() -> None:
    """Drop the views, schema and role; they hold no durable data (the base tables are untouched)."""
    op.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    op.execute("DO $$ BEGIN EXECUTE format('REVOKE ALL ON DATABASE %I FROM " + _ROLE + "', current_database()); END $$")
    # Roles are cluster-wide: another database (or a leftover grant) may still depend on this one, and a
    # failed DROP must not abort the downgrade of this database; leave the passwordless role in place.
    op.execute(
        "DO $$ BEGIN DROP ROLE IF EXISTS " + _ROLE + "; "
        "EXCEPTION WHEN dependent_objects_still_exist THEN NULL; END $$"
    )
