"""Datasource review backlog for ``query_collected_data``: SQL shape, flags, bounds and error mapping."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from modules.chat import db_query
from modules.chat.db_query import (
    CITATION_RULE,
    QueryCollectedDataArgs,
    _allowed_sources,
    _document_filters,
    _hot_cache_snapshots,
    _observation_filters,
    _with_flags,
)


def _sql(filters: list[object], literal: bool = True) -> str:
    """Compile filters to a PostgreSQL WHERE-ish string (literal binds unless the regconfig forbids it)."""
    from sqlalchemy import and_

    kwargs = {"literal_binds": True} if literal else {}
    return str(and_(*filters).compile(dialect=postgresql.dialect(), compile_kwargs=kwargs))


def _args(**kw: object) -> QueryCollectedDataArgs:
    return QueryCollectedDataArgs.model_validate(kw)


def test_citation_rule_covers_null_citation_rows() -> None:
    """Rows without a citation are usable but must not be given a number."""
    assert "citation is null" in CITATION_RULE


def test_legacy_aggregation_values_map_to_none() -> None:
    """Retired ``latest``/``timeseries`` still validate instead of erroring."""
    assert _args(aggregation="latest").aggregation == "none"
    assert _args(aggregation="timeseries").aggregation == "none"
    assert _args(aggregation="avg").aggregation == "avg"


def test_observation_filters_exact_and_index_friendly() -> None:
    """Provider is exact; metric/symbol compare case-insensitively on the column."""
    sql = _sql(
        _observation_filters(
            _args(providers=["Binance"], metrics=["Price"], symbols=["btcusdt"]),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
        )
    )
    assert "observations.provider IN ('binance')" in sql
    assert "lower(observations.metric) IN ('price')" in sql
    assert "upper(observations.symbol) IN ('BTCUSDT')" in sql
    assert "lower(observations.provider" not in sql


@pytest.mark.parametrize(
    ("kw", "expects_current"),
    [({"aggregation": "avg", "current_only": False}, True), ({"current_only": False}, False)],
)
def test_aggregates_always_use_current_revisions(
    kw: dict[str, object], expects_current: bool
) -> None:
    """Superseded revisions (is_current=false) must never feed an aggregate."""
    sql = _sql(
        _observation_filters(
            _args(**kw),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
        )
    )
    assert ("is_current IS true" in sql) is expects_current


def test_text_query_uses_stored_tsv_and_no_content_ilike() -> None:
    """Lexical match goes through document_chunks.content_tsv; title ILIKE only."""
    sql = _sql(
        _document_filters(
            _args(text_query="rate cut"),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
        ),
        literal=False,
    )
    assert "document_chunks.content_tsv @@" in sql
    assert "to_tsvector" not in sql
    assert "document_versions.content ILIKE" not in sql
    assert "documents.title ILIKE" in sql


@pytest.mark.asyncio
async def test_allowed_sources_requires_provider_and_reports_overflow() -> None:
    """The query requires provider IS NOT NULL and fetches one extra row to detect truncation."""
    seen: list[str] = []

    class _S:
        async def execute(self, stmt: object) -> SimpleNamespace:
            seen.append(
                str(
                    stmt.compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )  # type: ignore[attr-defined]
            )
            return SimpleNamespace(all=lambda: [])

    await _allowed_sources(
        _S(),  # type: ignore[arg-type]
        workspace_id=uuid4(),
        source_scope=None,
        allow_local_only=True,
    )
    assert "sources.provider IS NOT NULL" in seen[0]
    assert "LIMIT 101" in seen[0]


def test_with_flags_merges_only_when_present() -> None:
    """Truncation flags are merged into the JSON payload; empty flags leave it untouched."""
    assert _with_flags('{"a": 1}', {}) == '{"a": 1}'
    assert json.loads(_with_flags('{"a": 1}', {"scope_truncated": True})) == {
        "a": 1,
        "scope_truncated": True,
    }


@pytest.mark.asyncio
async def test_hot_cache_uses_single_mget() -> None:
    """Snapshot metadata is fetched with one MGET, skipping missing keys."""
    calls: list[list[str]] = []

    class _R:
        async def mget(self, keys: list[str]) -> list[bytes | None]:
            calls.append(keys)
            return [b'{"provider": "binance", "record_count": 3}', None]

    got = await _hot_cache_snapshots(_R(), uuid4(), [uuid4(), uuid4()])
    assert len(calls) == 1 and len(calls[0]) == 2
    assert got == [
        {"provider": "binance", "realtime_tier": None, "collected_at": None, "record_count": 3}
    ]


@pytest.mark.asyncio
async def test_reader_pool_timeout_maps_to_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool timeout becomes ChatSqlReaderBusyError, which the tool reports as sql_reader_busy."""

    class _Engine:
        def connect(self) -> _Engine:
            return self

        async def __aenter__(self) -> None:
            raise PoolTimeoutError("pool", None, None)

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(db_query, "_reader_engine", lambda: _Engine())
    with pytest.raises(db_query.ChatSqlReaderBusyError):
        await db_query._run_sql_as_reader(
            "SELECT 1",
            app_engine=None,  # type: ignore[arg-type]
            workspace_id=uuid4(),
            allowed_source_ids=[],
            max_rows=1,
        )


def test_bounded_scope_value_limits_strings_and_lists() -> None:
    """Gadget scope values are bounded before they reach the system prompt."""
    from modules.chat._public.conversations import bounded_scope_value
    from modules.chat._worker.setup import _bounded_scope_json

    assert len(bounded_scope_value("x" * 1000)) == 200
    got = bounded_scope_value(["y" * 500] * 50 + [{"n": 1}])
    assert len(got) == 20 and all(len(i) == 200 for i in got)
    assert len(_bounded_scope_json({"k": "z" * 5000, "l": list(range(100))})) <= 1000
