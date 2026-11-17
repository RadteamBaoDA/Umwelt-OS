"""Unit tests for chat record attributes, accent-insensitive search and series discovery."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.chat import _db_attributes as attrs
from modules.chat.db_query import (
    QueryCollectedDataArgs,
    _document_filters,
    _execute_structured_mode,
)


def test_bound_attributes_filters_and_bounds() -> None:
    """Columns, secrets, nested values dropped; strings cut; list capped; total size bounded."""
    out = attrs.bound_attributes(
        {
            "title": "x",
            "api_token": "t",
            "Secret": "s",
            "depth": 10.5,
            "obj": {"a": 1},
            "tags": ["a", {"b": 1}],
        },
        {"note": "n" * 500, "ok": ["a", "b"], "flag": True, "none": None},
    )
    assert out == {"depth": 10.5, "note": "n" * 200, "ok": ["a", "b"], "flag": True, "none": None}
    many = attrs.bound_attributes({f"k{i}": i for i in range(50)})
    assert len(many) == 20
    big = attrs.bound_attributes({f"k{i}": "v" * 200 for i in range(20)})
    assert len(json.dumps(big)) <= 1000 and 0 < len(big) < 20
    assert attrs.bound_attributes(None, "x") == {}


@pytest.mark.asyncio
async def test_fetch_attributes_is_one_query_and_merges_paths() -> None:
    """One SELECT for all versions; source_fields and provider_fields are merged; empty ids skip the DB."""
    v1, v2 = uuid4(), uuid4()
    calls: list[object] = []

    class _S:
        async def execute(self, stmt: object) -> SimpleNamespace:
            calls.append(stmt)
            return SimpleNamespace(all=lambda: [(v1, {"score": 3}, {"depth": 7}), (v2, None, None)])

    assert await attrs.fetch_attributes(_S(), []) == {}  # type: ignore[arg-type]
    assert not calls
    out = await attrs.fetch_attributes(_S(), [v1, v2, v1, None])  # type: ignore[arg-type]
    assert out == {v1: {"score": 3, "depth": 7}}
    assert len(calls) == 1
    sql = str(calls[0].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert sql.count("normalized_version_provenance.provenance[") == 2


def test_text_query_is_accent_insensitive_and_escapes_like() -> None:
    """Both the chunk tsquery and the title ILIKE go through f_unaccent; LIKE metacharacters are escaped."""
    filters = _document_filters(
        QueryCollectedDataArgs(text_query="giá 100%_x"),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=None,
    )
    sql = str(filters[-1].compile(dialect=postgresql.dialect()))
    assert sql.count("f_unaccent") >= 3
    assert "to_tsvector" in sql and "websearch_to_tsquery" in sql and "ILIKE" in sql
    assert attrs.escape_like("100%_a\\") == r"100\%\_a\\"


@pytest.mark.asyncio
async def test_empty_observations_list_available_series(monkeypatch: pytest.MonkeyPatch) -> None:
    """No observation rows: the payload carries available_series; documents are unaffected."""
    series = [{"provider": "usgs", "metric": "magnitude", "symbol": None, "region": "US"}]

    async def _series(*_a: object, **_k: object) -> list[dict[str, object]]:
        return series

    async def _reg(*_a: object, **_k: object) -> dict[object, int]:
        return {}

    async def _attrs(*_a: object, **_k: object) -> dict[object, dict[str, object]]:
        return {}

    async def _collect(*_a: object, **_k: object) -> tuple[list[object], list[object], bool]:
        return [], [], False

    from modules.chat import db_query

    monkeypatch.setattr(db_query, "available_series", _series)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", _reg)
    monkeypatch.setattr(db_query, "fetch_attributes", _attrs)
    monkeypatch.setattr(db_query, "_collect_observations", _collect)
    out = json.loads(
        await _execute_structured_mode(
            None,  # type: ignore[arg-type]
            QueryCollectedDataArgs(target="observations"),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
            evidence_list=[],
        )
    )
    assert out["available_series"] == series and out["observations"] == []


@pytest.mark.asyncio
async def test_available_series_query_is_scoped_current_and_capped() -> None:
    """The discovery SELECT is fenced to the workspace/sources, current rows only, limited to 50."""
    captured: list[object] = []

    class _S:
        async def execute(self, stmt: object) -> SimpleNamespace:
            captured.append(stmt)
            return SimpleNamespace(all=lambda: [])

    await attrs.available_series(
        _S(),  # type: ignore[arg-type]
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        source_scopes=None,
    )
    sql = str(captured[0].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert "workspace_id" in sql and "is_current" in sql and "GROUP BY" in sql
    assert "LIMIT" in sql
