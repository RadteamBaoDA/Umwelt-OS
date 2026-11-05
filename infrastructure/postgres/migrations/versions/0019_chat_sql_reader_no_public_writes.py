"""Remove PUBLIC write paths from the chat SQL reader login (defence in depth behind the parser allowlist).

``default_transaction_read_only`` on ``umwelt_chat_sql_login`` is only a default: a session can
``SET TRANSACTION READ WRITE``. PostgreSQL grants PUBLIC the database ``TEMPORARY`` privilege and EXECUTE on the
large-object functions (``lo_from_bytea`` creates a persistent large object even though no table is writable), on
``pg_notify`` and on the advisory-lock functions. This revokes those from PUBLIC so the reader has no write or
side-effect path of its own, and grants them back to the application role(s) that need them.

Decisions: temp tables and advisory locks are used by the application (granted back to the migration/owner role,
which is the application role); large objects and ``pg_notify`` are unused by the app (revoked, not granted back;
``lo_import``/``lo_export`` are already superuser-only and untouched); ``set_config`` is harmless since the 0018
fence no longer reads settings; ``pg_sleep`` is bounded by the role ``statement_timeout`` and left alone.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_chat_sql_reader_no_public_writes"
down_revision: str | Sequence[str] | None = "0018_chat_sql_query_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "umwelt_chat_sql_login"

# pg_catalog functions whose PUBLIC EXECUTE is revoked. Resolved by regprocedure so signatures are not hard-coded.
_REVOKED_FILTER = (
    "n.nspname = 'pg_catalog' AND (p.proname IN ('lo_create','lo_creat','lo_from_bytea','lo_put','lo_open',"
    "'lowrite','lo_unlink','lo_truncate','lo_truncate64','lo_get','loread','lo_lseek','lo_lseek64','lo_tell',"
    r"'lo_tell64','lo_close','pg_notify') OR p.proname LIKE 'pg\_%advisory\_%')"
)
_ADVISORY = r"p.proname LIKE 'pg\_%advisory\_%' AND n.nspname = 'pg_catalog'"
_FUNCTIONS = "SELECT p.oid::regprocedure AS f FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE "
# Application roles: the connecting migration/runtime user and the database owner (they may differ in some setups).
_APP_ROLES = "SELECT current_user::text AS r UNION SELECT pg_get_userbyid(datdba)::text FROM pg_database WHERE datname = current_database()"


def upgrade() -> None:
    """Revoke PUBLIC TEMPORARY and function EXECUTE, then grant the needed ones back to the application roles."""
    op.execute(
        "DO $$ DECLARE fn regprocedure; app text; BEGIN "
        "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
        f"EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM {_ROLE}', current_database()); "
        f"FOR fn IN {_FUNCTIONS}{_REVOKED_FILTER} LOOP "
        "EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', fn); "
        f"EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM {_ROLE}', fn); END LOOP; "
        f"FOR app IN {_APP_ROLES} LOOP "
        "EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), app); "
        f"FOR fn IN {_FUNCTIONS}{_ADVISORY} LOOP "
        "EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', fn, app); END LOOP; END LOOP; END $$"
    )


def downgrade() -> None:
    """Restore the stock PUBLIC grants and drop the explicit application-role grants."""
    op.execute(
        "DO $$ DECLARE fn regprocedure; app text; BEGIN "
        f"FOR app IN {_APP_ROLES} LOOP "
        "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM %I', current_database(), app); "
        f"FOR fn IN {_FUNCTIONS}{_ADVISORY} LOOP "
        "EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM %I', fn, app); END LOOP; END LOOP; "
        "EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC', current_database()); "
        f"FOR fn IN {_FUNCTIONS}{_REVOKED_FILTER} LOOP "
        "EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO PUBLIC', fn); END LOOP; END $$"
    )
