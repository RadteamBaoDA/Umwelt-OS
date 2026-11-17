"""Expose provider attributes and coordinates in the chat SQL views and add accent-insensitive search.

* ``chat_sql.scoped_observations`` gains ``latitude, longitude, attributes`` and ``chat_sql.scoped_documents``
  gains ``attributes`` (appended AFTER the 0024 columns, which is all ``CREATE OR REPLACE VIEW`` allows).
  ``attributes`` is the jsonb of ``provenance.provider_record.source_fields`` merged with
  ``provenance.provider_record.world_data.provider_fields`` for the row's current document version, minus
  credential-like keys (token/secret/password/key) and keys that duplicate a column. The 0024 fence joins
  and scope predicates are unchanged.
* ``public.f_unaccent(text)`` is an IMMUTABLE wrapper over the ``unaccent`` extension (the stock function is
  only STABLE, so it cannot back an index). A GIN index over
  ``to_tsvector('simple', f_unaccent(document_chunks.content))`` is built ``CONCURRENTLY`` so structured chat
  keyword search ignores Vietnamese accents. SQL mode is untouched: the reader role gets no EXECUTE grant
  decision here and the SQL guard allowlist does not include ``f_unaccent``.

``CREATE EXTENSION unaccent`` is a trusted extension (PostgreSQL 13+), so the database owner may create it.
Downgrade restores the exact 0024 view bodies, drops the index and the wrapper, and keeps the extension.
"""

from collections.abc import Sequence

from alembic import op

from core.migration_support import create_index_concurrently, drop_index_concurrently

revision: str = "0025_chat_attrs_unaccent"
down_revision: str | Sequence[str] | None = "0024_chat_sql_scope_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_document_chunks_content_unaccent_tsv"

_OBSERVATIONS_COLUMNS = """SELECT o.id, o.source_id, o.provider, o.metric, o.symbol, o.region, o.value, o.unit, o.currency,
       o.observed_at, o.published_at, o.quality, o.is_current, o.document_id, o.document_version_id"""
_OBSERVATIONS_FROM = "FROM public.observations o"
_DOCUMENTS_COLUMNS = """SELECT d.id, d.source_id, s.provider, d.title, d.canonical_url, d.published_at, d.observed_at,
       d.language, LEFT(dv.content, 600) AS content_excerpt, dv.id AS document_version_id"""
_DOCUMENTS_FROM = """FROM public.documents d
JOIN public.sources s ON s.id = d.source_id AND s.workspace_id = d.workspace_id
JOIN public.document_versions dv ON dv.document_id = d.id AND dv.version_number = d.current_version"""

_FENCE_JOIN = "JOIN chat_sql.query_fence f ON f.backend_pid = pg_backend_pid() AND f.expires_at > clock_timestamp()"
_SCOPE_JOIN = "JOIN chat_sql.query_fence_scope fs ON fs.backend_pid = f.backend_pid AND fs.source_id = {alias}.source_id"
_DOC_DISCRIMINATOR = (
    "(SELECT p.provenance ->> 'provider_scope_discriminator' "
    "FROM public.normalized_version_provenance p WHERE p.document_version_id = dv.id LIMIT 1)"
)
_OBS_WHERE = (
    "WHERE o.workspace_id = f.workspace_id AND o.source_id = ANY(f.source_ids) "
    "AND o.is_current AND o.provider_scope_discriminator IS NOT DISTINCT FROM fs.discriminator"
)
_DOC_WHERE = (
    "WHERE d.workspace_id = f.workspace_id AND d.source_id = ANY(f.source_ids) "
    f"AND {_DOC_DISCRIMINATOR} IS NOT DISTINCT FROM fs.discriminator"
)


def _attributes(version_id: str) -> str:
    """SQL scalar subquery: bounded-by-the-cell-cap jsonb of the version's provider facts, secrets stripped."""
    obj = "CASE WHEN jsonb_typeof({x}) = 'object' THEN {x} ELSE '{{}}'::jsonb END"
    source = obj.format(x="p.provenance #> '{provider_record,source_fields}'")
    measured = obj.format(x="p.provenance #> '{provider_record,world_data,provider_fields}'")
    return (
        "(SELECT COALESCE(jsonb_object_agg(kv.key, kv.value), '{}'::jsonb) "  # noqa: S608  # code-only fragments
        f"FROM public.normalized_version_provenance p, LATERAL jsonb_each(({source}) || ({measured})) kv "
        f"WHERE p.document_version_id = {version_id} "
        "AND kv.key !~* '(token|secret|password|key)' "
        "AND kv.key NOT IN ('title', 'summary', 'body', 'canonical_url', 'published_at')) AS attributes"
    )


def _view(name: str, body: str) -> str:
    """Return ``CREATE OR REPLACE VIEW`` DDL for a ``security_barrier`` ``chat_sql`` view."""
    return f"CREATE OR REPLACE VIEW chat_sql.{name} WITH (security_barrier = true) AS\n{body}"


def _observations_view(extra_columns: str) -> str:
    """Observation view DDL: 0024 columns, ``extra_columns`` appended, 0024 joins and predicates unchanged."""
    return _view(
        "scoped_observations",
        f"{_OBSERVATIONS_COLUMNS}{extra_columns}\n{_OBSERVATIONS_FROM}\n{_FENCE_JOIN}\n"
        f"{_SCOPE_JOIN.format(alias='o')}\n{_OBS_WHERE}",
    )


def _documents_view(extra_columns: str) -> str:
    """Document view DDL: 0024 columns, ``extra_columns`` appended, 0024 joins and predicates unchanged."""
    return _view(
        "scoped_documents",
        f"{_DOCUMENTS_COLUMNS}{extra_columns}\n{_DOCUMENTS_FROM}\n{_FENCE_JOIN}\n"
        f"{_SCOPE_JOIN.format(alias='d')}\n{_DOC_WHERE}",
    )


def upgrade() -> None:
    """Add the unaccent wrapper and views (transactional), then build the GIN index concurrently."""
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA public")
    op.execute(
        "CREATE OR REPLACE FUNCTION public.f_unaccent(text) RETURNS text "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT SET search_path = public, pg_temp "
        "AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$"
    )
    op.execute(
        _observations_view(f", o.latitude, o.longitude, {_attributes('o.document_version_id')}")
    )
    op.execute(_documents_view(f", {_attributes('dv.id')}"))
    create_index_concurrently(
        _INDEX,
        "document_chunks",
        "USING gin (to_tsvector('simple', public.f_unaccent(content)))",
    )


def downgrade() -> None:
    """Restore the 0024 view bodies, then drop the index and the wrapper (the extension stays)."""
    op.execute(_observations_view(""))
    op.execute(_documents_view(""))
    drop_index_concurrently(_INDEX)
    op.execute("DROP FUNCTION IF EXISTS public.f_unaccent(text)")
