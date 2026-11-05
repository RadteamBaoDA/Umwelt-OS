"""Characterization of entity ``export_page`` bounds, derived-field blanking, fences and paging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.knowledge.entities import public as ent
from tests.unit.entity_patching import patch_entities

NOW = datetime(2026, 5, 1, tzinfo=UTC)
WS = uuid4()


def _scope() -> Any:
    """Owner workspace context for user 7."""
    return SimpleNamespace(workspace_id=WS, user_id=7, role="owner")


def _entity(n: int, *, owner: bool = False) -> Any:
    """Entity row stand-in; ``owner`` makes name/description owner-origin (exported without support)."""
    origin = "owner" if owner else "derived"
    return SimpleNamespace(
        id=uuid4(), type="person", name=f"Name {n}", canonical_name=f"name {n}", description="desc", revision=2,
        name_origin=origin, description_origin=origin, first_seen_at=None, last_seen_at=None,
        created_at=NOW + timedelta(seconds=n), updated_at=NOW + timedelta(seconds=n),
    )


def _alias(entity: Any) -> Any:
    """Alias row stand-in."""
    return SimpleNamespace(id=uuid4(), entity_id=entity.id, alias="al", confirmed=True, origin="owner",
                           confidence=None, created_at=NOW)


def _membership(source_id: Any) -> Any:
    """Evidence membership stand-in."""
    return SimpleNamespace(id=uuid4(), source_id=source_id, document_id=uuid4(), document_version_id=uuid4(),
                           chunk_id=uuid4(), observed_at=NOW, extracted_at=NOW, confidence=0.5)


def _session(rows: list[Any], children: dict[Any, tuple[list[Any], list[Any]]], support: list[Any] | None = None) -> Any:
    """Session whose scalars answer: entity page, then per-entity aliases and evidence in order."""
    queue: list[list[Any]] = [rows]
    for row in rows:
        aliases, evidence = children.get(row.id, ([], []))
        queue.extend([aliases, evidence])

    async def scalars(_statement: Any) -> Any:
        batch = queue.pop(0)
        return SimpleNamespace(all=lambda: batch)

    async def execute(_statement: Any) -> Any:
        return SimpleNamespace(all=lambda: support or [])

    return SimpleNamespace(scalars=AsyncMock(side_effect=scalars), execute=AsyncMock(side_effect=execute))


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Neutralize admission, counts, source generations and field-support checks."""
    calls = SimpleNamespace(field_checks=[], generations=[])
    patch_entities(monkeypatch, "_admit", AsyncMock())
    patch_entities(monkeypatch, "_actor", lambda _scope: 7)
    patch_entities(monkeypatch, "_entity_export_count", AsyncMock(return_value=5))
    monkeypatch.setattr(ent.sources, "export_eligible_source_ids", lambda **_k: [])

    async def generations(session: Any, ids: set[Any], **_k: Any) -> dict[Any, int]:
        calls.generations.append(set(ids))
        return {i: 4 for i in ids}

    async def supported(session: Any, entity: Any, field: str, value: Any, **_k: Any) -> bool:
        calls.field_checks.append((entity.id, field))
        return field == "name"

    patch_entities(monkeypatch, "_entity_export_source_generations", generations)
    patch_entities(monkeypatch, "_entity_export_field_is_supported", supported)
    return calls


async def _export(session: Any, **over: Any) -> Any:
    """Call ``export_page`` with defaults."""
    kwargs: dict[str, Any] = {"owner_id": 7, "record_kind": "entities", "limit": 50, "scope": _scope(), "multi_workspace_enabled": False}
    kwargs.update(over)
    return await ent.export_page(session, **kwargs)


async def test_invalid_kind_owner_or_limit_is_rejected() -> None:
    """Wrong record kind, foreign owner or out-of-range limit raise the stable ValueError."""
    for over in ({"record_kind": "x"}, {"owner_id": 8}, {"limit": 0}, {"limit": 101}):
        with pytest.raises(ValueError, match="Entity export kind or page limit is invalid"):
            await _export(_session([], {}), **over)


async def test_owner_origin_entity_exports_with_children_fence_and_no_field_checks(_patched: SimpleNamespace) -> None:
    """Owner-origin fields skip support checks; aliases, citations and source fences are exported together."""
    row = _entity(1, owner=True)
    source = uuid4()
    alias, evidence = _alias(row), _membership(source)
    support = [(SimpleNamespace(confidence=0.9), _membership(source))]
    page = await _export(_session([row], {row.id: ([alias], [evidence])}, support))
    assert page.snapshot_count == 5 and page.next_cursor is None and page.owner_id == 7
    item, fence = page.items[0], page.fences[0]
    assert item.name == "Name 1" and item.name_origin == "owner" and item.description == "desc"
    assert [a.id for a in item.aliases] == [alias.id] and item.aliases[0].supports[0].source_generation == 4
    assert item.evidence[0].source_generation == 4
    assert fence.alias_ids == [alias.id] and fence.evidence_ids == [evidence.id]
    assert {(f.source_id, f.generation) for f in fence.source_fences} == {(source, 4)}
    assert len(fence.alias_digest) == 64 and len(fence.evidence_digest) == 64
    assert _patched.field_checks == []
    assert page.payload_bytes > 0


async def test_derived_fields_without_support_are_blanked() -> None:
    """Non-owner name stays only when supported; unsupported description is dropped with its origin."""
    row = _entity(1)
    page = await _export(_session([row], {}))
    item = page.items[0]
    assert (item.name, item.name_origin) == ("Name 1", "derived")
    assert (item.description, item.description_origin) == (None, None)


async def test_extra_row_sets_cursor_at_last_item() -> None:
    """limit+1 rows trim to the limit and emit a cursor positioned after the last returned entity."""
    rows = [_entity(n, owner=True) for n in range(3)]
    page = await _export(_session(rows, {}), limit=2)
    assert [i.id for i in page.items] == [rows[0].id, rows[1].id] and page.next_cursor is not None
    snapshot_at, position_at, position_id = ent._decode_entity_export_cursor(page.next_cursor, 7, WS)
    assert (position_at, position_id) == (rows[1].created_at, rows[1].id) and snapshot_at == page.snapshot_at


async def test_bounds_raise_explicit_errors() -> None:
    """Over-100 aliases/citations fail loudly instead of truncating."""
    row = _entity(1, owner=True)
    too_many = [_alias(row) for _ in range(101)]
    with pytest.raises(ValueError, match="alias or citation reference bound"):
        await _export(_session([row], {row.id: (too_many, [])}))
    many_sources = [_membership(uuid4()) for _ in range(101)]
    with pytest.raises(ValueError, match="alias or citation reference bound"):
        await _export(_session([row], {row.id: ([], many_sources)}))
    # 100 citation sources plus one alias-support source is 101 distinct sources.
    alias = _alias(row)
    support = [(SimpleNamespace(confidence=0.1), _membership(uuid4()))]
    evidence = [_membership(uuid4()) for _ in range(100)]
    with pytest.raises(ValueError, match="distinct source generation bound"):
        await _export(_session([row], {row.id: ([alias], evidence)}, support))
