"""Fence the chat SQL views by the CURRENT provider scope and current revisions.

Source configuration replace changes the provider scope discriminator, and pause/resume bumps
``sources.generation``. Chat reads active AND paused sources, so it cannot filter by generation; it
filters by the discriminator of the source's current configuration instead. The application writes one
``chat_sql.query_fence_scope`` row per (reader backend pid, source) in the same committed transaction as
the ``query_fence`` row, and the views join it: an observation must carry the matching
``provider_scope_discriminator`` and be ``is_current``; a document's current version must carry the same
discriminator in ``normalized_version_provenance`` (a NULL discriminator matches documents with none).
Same column lists as 0018 (``CREATE OR REPLACE VIEW`` cannot change columns). The reader has no privilege
on the new table; the views are owned by the migration user.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_chat_sql_scope_fence"
down_revision: str | Sequence[str] | None = "0023_obs_source_observed_idx"
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
_SCOPE_JOIN = "JOIN chat_sql.query_fence_scope fs ON fs.backend_pid = f.backend_pid AND fs.source_id = {alias}.source_id"
_DOC_DISCRIMINATOR = (
    "(SELECT p.provenance ->> 'provider_scope_discriminator' "
    "FROM public.normalized_version_provenance p WHERE p.document_version_id = dv.id LIMIT 1)"
)


def _view(name: str, body: str) -> str:
    """Return ``CREATE OR REPLACE VIEW`` DDL for a ``security_barrier`` ``chat_sql`` view."""
    return f"CREATE OR REPLACE VIEW chat_sql.{name} WITH (security_barrier = true) AS\n{body}"


def upgrade() -> None:
    """Create the per-source scope fence table and repoint both views at it."""
    op.execute(
        "CREATE TABLE chat_sql.query_fence_scope ("
        "backend_pid integer NOT NULL, "
        "source_id uuid NOT NULL, "
        "discriminator text, "
        "PRIMARY KEY (backend_pid, source_id))"
    )
    op.execute("REVOKE ALL ON chat_sql.query_fence_scope FROM PUBLIC")
    op.execute(f"REVOKE ALL ON chat_sql.query_fence_scope FROM {_ROLE}")
    op.execute(
        _view(
            "scoped_observations",
            f"{_OBSERVATIONS_COLUMNS}\n{_FENCE_JOIN}\n{_SCOPE_JOIN.format(alias='o')}\n"
            "WHERE o.workspace_id = f.workspace_id AND o.source_id = ANY(f.source_ids) "
            "AND o.is_current AND o.provider_scope_discriminator IS NOT DISTINCT FROM fs.discriminator",
        )
    )
    op.execute(
        _view(
            "scoped_documents",
            f"{_DOCUMENTS_COLUMNS}\n{_FENCE_JOIN}\n{_SCOPE_JOIN.format(alias='d')}\n"
            "WHERE d.workspace_id = f.workspace_id AND d.source_id = ANY(f.source_ids) "
            f"AND {_DOC_DISCRIMINATOR} IS NOT DISTINCT FROM fs.discriminator",
        )
    )


def downgrade() -> None:
    """Restore the exact 0018 view bodies, then drop the scope table (rows are transient)."""
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
    op.execute("DROP TABLE IF EXISTS chat_sql.query_fence_scope")
