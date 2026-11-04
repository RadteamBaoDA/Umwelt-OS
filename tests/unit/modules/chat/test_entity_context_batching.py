"""Entity-context batching: the retrieval path uses three bulk owner reads (no per-entity N+1) and
falls back to the per-entity path with identical results when the batch fails."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.chat import retrieval
from modules.chat.retrieval import _RefCollector, _resolve_entity_context


def _entity(entity_id, name="Acme"):
    return SimpleNamespace(
        id=entity_id, name=name, canonical_name=name.lower(), type="company", description="d",
    )


def _evidence(document_version_id, chunk_id):
    return SimpleNamespace(
        document_version_id=document_version_id, chunk_id=chunk_id, source_id=uuid4(), document_id=uuid4(),
        title="T", canonical_url=None, observed_at=datetime(2026, 1, 1, tzinfo=UTC), excerpt="quote",
    )


def _neighbor(target_id):
    return SimpleNamespace(
        relationship=SimpleNamespace(id=uuid4(), type="owns"), entity=SimpleNamespace(id=target_id, name="Beta"),
    )


def _request(ids, selected_only=False):
    return SimpleNamespace(entity_ids=ids, selected_only=selected_only)


async def _resolve_or_raise(entity_id, bad):
    if entity_id == bad:
        raise ValueError("Entity redirect cycle detected")
    return entity_id


@pytest.fixture
def owners(monkeypatch):
    """Patch both owner modules with AsyncMocks and return them for call-count assertions."""
    entities = SimpleNamespace(
        get_entities=AsyncMock(), list_entity_evidence_bulk=AsyncMock(),
        resolve_canonical_entity_id=AsyncMock(), get_entity=AsyncMock(), list_entity_evidence=AsyncMock(),
    )
    relationships = SimpleNamespace(get_neighbors_bulk=AsyncMock(), get_neighbors=AsyncMock())
    monkeypatch.setattr(retrieval, "entities_public", entities)
    monkeypatch.setattr(retrieval, "relationships_public", relationships)
    return entities, relationships


@pytest.mark.asyncio
async def test_many_entities_use_exactly_three_bulk_reads(owners) -> None:
    entities, relationships = owners
    ids = [uuid4() for _ in range(40)]
    ref = (uuid4(), uuid4())
    entities.get_entities.return_value = [_entity(i, f"E{n}") for n, i in enumerate(ids)]
    entities.list_entity_evidence_bulk.return_value = [SimpleNamespace(items=[_evidence(*ref)]) for _ in ids]
    relationships.get_neighbors_bulk.return_value = [SimpleNamespace(items=[_neighbor(uuid4())]) for _ in ids]
    collector = _RefCollector()

    items = await _resolve_entity_context(AsyncMock(), _request(ids), {"scope": "s"}, collector)

    assert [i.entity_id for i in items] == ids and items[3].name == "E3"
    assert entities.get_entities.await_count == 1
    assert entities.list_entity_evidence_bulk.await_count == 1
    assert relationships.get_neighbors_bulk.await_count == 1
    assert entities.resolve_canonical_entity_id.await_count == 0 and entities.get_entity.await_count == 0
    assert collector.refs == [ref]  # de-duplicated, first-seen order
    assert entities.list_entity_evidence_bulk.await_args.kwargs == {"limit": 10, "scope": "s"}
    assert items[0].neighbors[0]["type"] == "owns" and items[0].backing_refs[0].quote == "quote"


@pytest.mark.asyncio
async def test_missing_entities_are_skipped_and_bulk_gets_only_present_ids(owners) -> None:
    entities, relationships = owners
    a, b = uuid4(), uuid4()
    entities.get_entities.return_value = [None, _entity(b)]
    entities.list_entity_evidence_bulk.return_value = [None]
    relationships.get_neighbors_bulk.return_value = [None]

    items = await _resolve_entity_context(AsyncMock(), _request([a, b]), {}, _RefCollector())

    assert [i.entity_id for i in items] == [b]
    assert entities.list_entity_evidence_bulk.await_args.args[1] == [b]
    assert items[0].backing_refs == [] and items[0].neighbors == []


@pytest.mark.asyncio
async def test_bulk_failure_falls_back_to_per_entity_resolution_and_isolates_errors(owners) -> None:
    entities, relationships = owners
    good, bad = uuid4(), uuid4()
    entities.get_entities.side_effect = ValueError("Entity redirect cycle detected")

    async def resolve(_session, entity_id, **_kw):
        return await _resolve_or_raise(entity_id, bad)

    entities.resolve_canonical_entity_id.side_effect = resolve
    entities.get_entity.return_value = _entity(good)
    entities.list_entity_evidence.return_value = SimpleNamespace(items=[])
    relationships.get_neighbors.return_value = None

    items = await _resolve_entity_context(AsyncMock(), _request([bad, good]), {}, _RefCollector())

    assert [i.entity_id for i in items] == [good]  # the bad entity is logged and skipped
    assert entities.resolve_canonical_entity_id.await_count == 2


@pytest.mark.asyncio
async def test_type_error_in_bulk_path_is_not_swallowed(owners) -> None:
    entities, _ = owners
    entities.get_entities.side_effect = TypeError("bad call shape")
    with pytest.raises(TypeError):
        await _resolve_entity_context(AsyncMock(), _request([uuid4()]), {}, _RefCollector())


@pytest.mark.asyncio
async def test_selected_only_and_empty_requests_read_nothing(owners) -> None:
    entities, _ = owners
    assert await _resolve_entity_context(
        AsyncMock(), _request([uuid4()], selected_only=True), {}, _RefCollector()) == []
    assert await _resolve_entity_context(AsyncMock(), _request([]), {}, _RefCollector()) == []
    assert entities.get_entities.await_count == 0
