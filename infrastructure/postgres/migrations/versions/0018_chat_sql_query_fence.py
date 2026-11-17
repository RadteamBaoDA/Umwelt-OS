"""Make the chat SQL fence independent of anything the reader login can change.

0017 fenced ``chat_sql.scoped_observations`` / ``scoped_documents`` with ``current_setting('umwelt.*')``,
which the reader session itself sets (and, if model SQL escaped the parser allowlist, could retarget).
The views now join ``chat_sql.query_fence``: one row per reader backend pid, written by the application
connection (never the reader) immediately before a query and deleted right after, with a short
``expires_at`` so a crash cannot leave a long-lived fence. The reader has no privilege on the table; the
views are owned by the migration user, so their bodies read it with owner rights.

``pg_stat_activity.backend_start`` is not used to pin the exact session: the view owner cannot see another
role's session details (columns read NULL), so the pid plus the expiry bound is the pin. A reused pid can
only inherit a not-yet-expired fence of the same application, for at most the expiry.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_chat_sql_query_fence"
down_revision: str | Sequence[str] | None = "0017_chat_sql_login_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "umwelt_chat_sql_login"

_OBSERVATIONS_COLUMNS = """SELECT o.id, o.source_id, o.provider, o.metric, o.symbol, o.region, o.value, o.unit, o.currency,
       o.observed_at, o.published_at, o.quality, o.is_current, o.document_id, o.document_version_id
FROM public.observations o"""
_DOCUMENTS_COLUMNS = """SELECT d.id, d.source_id, s.provider, d.title, d.canonical_url, d.published_at, d.observed_at,
       d.language, LEFT(dv.content, 600) AS content_excerpt, dv.id AS document_version_id
FROM public.documents d
JOIN public.sources s ON s.id = d.source_id AND s.workspace_id = d.workspace_id
JOIN public.document_versions dv ON dv.document_id = d.id AND dv.version_number = d.current_version"""

_FENCE_JOIN = "JOIN chat_sql.query_fence f ON f.backend_pid = pg_backend_pid() AND f.expires_at > clock_timestamp()"

_OLD_OBSERVATIONS_WHERE = (
    "WHERE o.workspace_id = NULLIF(current_setting('umwelt.chat_sql_workspace', true), '')::uuid AND o.source_id = "
    "ANY(string_to_array(NULLIF(current_setting('umwelt.chat_sql_sources', true), ''), ',')::uuid[])"
)
_OLD_DOCUMENTS_WHERE = _OLD_OBSERVATIONS_WHERE.replace("o.", "d.")


def _view(name: str, body: str) -> str:
    """Return ``CREATE OR REPLACE VIEW`` DDL for a ``security_barrier`` ``chat_sql`` view."""
    return f"CREATE OR REPLACE VIEW chat_sql.{name} WITH (security_barrier = true) AS\n{body}"


def upgrade() -> None:
    """Create the fence table (no reader privileges) and repoint both views at it."""
    op.execute(
        "CREATE TABLE chat_sql.query_fence ("
        "backend_pid integer PRIMARY KEY, "
        "workspace_id uuid NOT NULL, "
        "source_ids uuid[] NOT NULL, "
        "expires_at timestamptz NOT NULL)"
    )
    op.execute("REVOKE ALL ON chat_sql.query_fence FROM PUBLIC")
    op.execute(f"REVOKE ALL ON chat_sql.query_fence FROM {_ROLE}")
    op.execute(
        _view(
            "scoped_observations",
            f"{_OBSERVATIONS_COLUMNS}\n{_FENCE_JOIN}\nWHERE o.workspace_id = f.workspace_id AND o.source_id = ANY(f.source_ids)",
        )
    )
    op.execute(
        _view(
            "scoped_documents",
            f"{_DOCUMENTS_COLUMNS}\n{_FENCE_JOIN}\nWHERE d.workspace_id = f.workspace_id AND d.source_id = ANY(f.source_ids)",
        )
    )


def downgrade() -> None:
    """Restore the 0017 GUC-based view definitions, then drop the fence table (rows are transient)."""
    op.execute(_view("scoped_observations", f"{_OBSERVATIONS_COLUMNS}\n{_OLD_OBSERVATIONS_WHERE}"))
    op.execute(_view("scoped_documents", f"{_DOCUMENTS_COLUMNS}\n{_OLD_DOCUMENTS_WHERE}"))
    op.execute("DROP TABLE IF EXISTS chat_sql.query_fence")
