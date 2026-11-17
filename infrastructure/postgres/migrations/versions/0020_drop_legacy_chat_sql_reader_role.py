"""Drop the unused 0013 ``umwelt_chat_sql_reader`` role and move 0019's app grants onto a ``umwelt_app`` group role.

The chat SQL tool connects as ``umwelt_chat_sql_login`` (0017); the NOLOGIN ``umwelt_chat_sql_reader`` role
from 0013 is no longer referenced by code, so it is revoked from its members and dropped.

0019 granted ``TEMPORARY`` and advisory-lock ``EXECUTE`` only to ``current_user`` and the database owner, which
breaks deployments whose runtime role differs from the migration role. This migration introduces the NOLOGIN
group role ``umwelt_app`` that holds those privileges and is granted to ``current_user`` and the database
owner. Operators with a distinct runtime role must ``GRANT umwelt_app TO <runtime role>``
(``python -m core.chat_sql_provision`` does it when ``DATABASE_URL`` and the admin URL name different users).
The reader login is never a member. The 0019 direct grants are left in place so the downgrade restores 0019.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_drop_legacy_chat_sql_reader_role"
down_revision: str | Sequence[str] | None = "0019_chat_sql_reader_no_public_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY = "umwelt_chat_sql_reader"
_GROUP = "umwelt_app"
_ADVISORY = r"p.proname LIKE 'pg\_%advisory\_%' AND n.nspname = 'pg_catalog'"
_FUNCTIONS = "SELECT p.oid::regprocedure AS f FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE "
_MEMBERS = (
    "SELECT current_user::text AS r UNION SELECT pg_get_userbyid(datdba)::text FROM pg_database "
    "WHERE datname = current_database()"
)


def upgrade() -> None:
    """Drop the legacy role (idempotent) and create/grant the ``umwelt_app`` group role."""
    op.execute(
        "DO $$ DECLARE m text; BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _LEGACY + "') THEN "  # noqa: S608  # module constant
        "FOR m IN SELECT pg_get_userbyid(member)::text FROM pg_auth_members WHERE roleid = '" + _LEGACY + "'::regrole LOOP "
        "EXECUTE format('REVOKE " + _LEGACY + " FROM %I', m); END LOOP; "
        "BEGIN DROP ROLE " + _LEGACY + "; EXCEPTION WHEN dependent_objects_still_exist THEN NULL; END; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ DECLARE fn regprocedure; app text; BEGIN "  # noqa: S608  # module constants
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _GROUP + "') THEN "  # module constant
        "CREATE ROLE " + _GROUP + " NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; "
        "EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO " + _GROUP + "', current_database()); "
        f"FOR fn IN {_FUNCTIONS}{_ADVISORY} LOOP "
        "EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO " + _GROUP + "', fn); END LOOP; "
        f"FOR app IN {_MEMBERS} LOOP "
        "EXECUTE format('GRANT " + _GROUP + " TO %I', app); END LOOP; END $$"
    )


def downgrade() -> None:
    """Drop ``umwelt_app`` (0019's direct grants remain) and recreate the legacy role exactly as 0013 did."""
    op.execute(
        "DO $$ DECLARE fn regprocedure; BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _GROUP + "') THEN "  # noqa: S608  # module constant
        "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM " + _GROUP + "', current_database()); "
        f"FOR fn IN {_FUNCTIONS}{_ADVISORY} LOOP "
        "EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM " + _GROUP + "', fn); END LOOP; "
        "BEGIN DROP ROLE " + _GROUP + "; EXCEPTION WHEN dependent_objects_still_exist THEN NULL; END; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _LEGACY + "') THEN "  # noqa: S608  # module constant
        "CREATE ROLE " + _LEGACY + " NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS; END IF; END $$"
    )
    op.execute(
        f"ALTER ROLE {_LEGACY} NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    )
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_LEGACY}")
    op.execute("DO $$ BEGIN EXECUTE format('REVOKE ALL ON DATABASE %I FROM " + _LEGACY + "', current_database()); END $$")
    op.execute(f"GRANT {_LEGACY} TO CURRENT_USER")
