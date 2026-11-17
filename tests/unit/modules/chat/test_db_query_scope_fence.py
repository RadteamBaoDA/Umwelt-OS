"""Chat reads data collected under each source's CURRENT scope, deduped across sources."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from modules.chat import db_query
from modules.chat.db_query import QueryCollectedDataArgs


def _sql(expr: object) -> str:
    """Compile an expression with the PostgreSQL dialect and inline literals."""
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


def _filters(scopes: dict | None, **kw: object) -> list:
    args = QueryCollectedDataArgs(**kw)  # type: ignore[arg-type]
    return db_query._observation_filters(
        args,
        workspace_id=uuid4(),
        allowed_source_ids=list(scopes or {}),
        date_from=datetime(2026, 1, 1, tzinfo=UTC),
        date_to=None,
        source_scopes=scopes,
    )


def test_observations_match_source_and_discriminator_pairs_and_skip_unscoped() -> None:
    """Each source is bound to its own discriminator; a source without scope contributes nothing."""
    a, b, none_src = uuid4(), uuid4(), uuid4()
    sql = " AND ".join(_sql(f) for f in _filters({a: "a" * 64, b: "b" * 64, none_src: None}))
    assert f"'{'a' * 64}'" in sql and f"'{'b' * 64}'" in sql
    assert (
        str(none_src) not in sql.split("provider_scope_discriminator", 1)[1].split("is_current")[0]
    )
    assert "NOT (EXISTS" in sql and "accepted_at" in sql  # cross-source dedup
    assert "is_current IS true" in sql


def test_no_scoped_sources_matches_nothing() -> None:
    """An empty scope mapping is fail closed."""
    assert "false" in " ".join(_sql(f) for f in _filters({})).lower()


def test_history_mode_skips_dedup_and_aggregate_shares_filters() -> None:
    """current_only=False keeps revisions (no dedup, no is_current); aggregation forces both."""
    src = {uuid4(): "a" * 64}
    assert "NOT (EXISTS" not in " ".join(_sql(f) for f in _filters(src, current_only=False))
    assert "NOT (EXISTS" in " ".join(
        _sql(f) for f in _filters(src, current_only=False, aggregation="avg")
    )


def test_document_scope_filter_uses_null_safe_provenance_match() -> None:
    """Documents compare the version provenance discriminator null-safely, per source."""
    src = uuid4()
    sql = _sql(select(db_query.Document.id).where(db_query.document_scope_filter({src: None})))
    assert "IS NOT DISTINCT FROM NULL" in sql and "provider_scope_discriminator" in sql
    assert "false" in _sql(select(1).where(db_query.document_scope_filter({}))).lower()


def test_readable_statuses_include_paused_but_not_archived() -> None:
    """Chat reads active and paused sources only."""
    assert db_query._CHAT_READABLE_STATUSES == ("active", "paused")
