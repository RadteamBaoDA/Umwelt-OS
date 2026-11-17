"""Harden a pre-existing ``umwelt_app`` group role and fail if the chat SQL reader can reach it.

0020 was edited after it was first released, so databases already at 0020 never ran its hardening. This
migration applies it once more for every database: it ensures ``umwelt_app`` exists, forces the safe role
attributes (a pre-existing role of that name is not trusted), detaches the reader login and, as the last
step, aborts if the reader is still a direct or indirect member. It performs no grants, so the membership
assertion runs after everything that could have added a member.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_harden_umwelt_app_group_role"
down_revision: str | Sequence[str] | None = "0020_drop_legacy_chat_sql_reader_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP = "umwelt_app"
_READER = "umwelt_chat_sql_login"


def upgrade() -> None:
    """Idempotently create/harden ``umwelt_app``, revoke it from the reader and assert non-membership."""
    op.execute(
        "DO $$ BEGIN "  # noqa: S608  # module constants
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _GROUP + "') THEN "
        "CREATE ROLE " + _GROUP + " NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; "
        "ALTER ROLE " + _GROUP + " NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + _READER + "') THEN "
        "REVOKE " + _GROUP + " FROM " + _READER + "; "
        # Last on purpose: any grant above would otherwise be able to re-add the reader unnoticed.
        "IF pg_has_role('" + _READER + "', '" + _GROUP + "', 'MEMBER') THEN "
        "RAISE EXCEPTION '" + _READER + " must not be a direct or indirect member of " + _GROUP + "'; END IF; "
        "END IF; END $$"
    )


def downgrade() -> None:
    """No-op: role attribute hardening is deliberately not reversed."""
