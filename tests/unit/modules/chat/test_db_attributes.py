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


def test_serialize_observations_labels_rows_with_source_name() -> None:
    """A coordinate-keyed series (open_meteo) is findable by the owner's source name ("Hà Nội")."""
    from datetime import UTC, datetime

    src, version = uuid4(), uuid4()
    row = SimpleNamespace(
        provider="open_meteo",
        metric="temperature_2m",
        symbol=None,
        region="21.03,105.85",
        value=31.2,
        unit="°C",
        currency=None,
        latitude=21.03,
        longitude=105.85,
        observed_at=datetime(2026, 11, 17, tzinfo=UTC),
        is_current=True,
        document_version_id=version,
        source_id=src,
    )
    [out] = attrs.serialize_observations([row], {version: 2}, {}, {src: "Hà Nội"})
    assert out["source_name"] == "Hà Nội" and out["citation"] == "[2]"
    assert attrs.serialize_observations([row], {}, {})[0]["source_name"] is None


def test_phrase_skeleton_regex_matches_abbreviated_region_titles() -> None:
    """P6: "Maluku Barat Daya" matches the compacted BMKG title but not the other Maluku region."""
    import re

    from modules.chat._db_attributes import phrase_skeleton_regex

    def compact(title: str) -> str:
        return re.sub(r"[^a-z]", "", title.lower())

    rx = phrase_skeleton_regex("Maluku Barat Daya")
    assert rx is not None
    assert re.search(rx, compact("43 km BaratLaut MALUKUBRTDAYA"))
    assert re.search(rx, compact("Gempa di Maluku Barat Daya"))
    assert not re.search(rx, compact("130 km BaratDaya MALUKUTENGGARA"))
    papua = phrase_skeleton_regex("Papua Barat Daya")
    assert papua is not None
    assert re.search(papua, compact("241 km TimurLaut RAJAAMPAT-PAPUABRTDY"))
    assert not re.search(papua, compact("75 km BaratLaut INTANJAYA-PAPUATENGAH"))


def test_phrase_skeleton_regex_bounds() -> None:
    """P6: single words, 7+ words and very short phrases yield no phrase arm."""
    from modules.chat._db_attributes import phrase_skeleton_regex

    assert phrase_skeleton_regex("Maluku") is None
    assert phrase_skeleton_regex("a b c d e f g") is None
    assert phrase_skeleton_regex("to do") is None
    assert phrase_skeleton_regex("   ") is None


def test_text_query_phrase_arm_is_parameterized_and_scoped() -> None:
    """P6: the phrase arm binds its pattern as a parameter and sits behind the scope predicates."""
    filters = _document_filters(
        QueryCollectedDataArgs(text_query="Maluku Barat Daya'; DROP"),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=None,
    )
    assert len(filters) == 3
    compiled = filters[-1].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "regexp_replace" in sql and " ~ " in sql
    assert "DROP" not in sql
    assert any("[aeiou]*" in str(v) for v in compiled.params.values())
