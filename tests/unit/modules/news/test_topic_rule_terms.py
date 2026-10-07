"""P15-T6a: topic term resolution and live-topic lookup used by dashboard highlight rules."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.news import topics
from modules.news.topics import live_topic_ids, resolve_topic_terms


def sql_text(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class CaptureSession:
    """Records statements; returns canned scalars rows."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows, self.statements = rows, []

    async def scalars(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_live_topic_ids_filters_owner_and_deleted() -> None:
    topic = uuid4()
    session = CaptureSession([topic])
    assert await live_topic_ids(session, 7, [topic]) == {topic}  # type: ignore[arg-type]
    sql = sql_text(session.statements[0])
    assert "owner_id = 7" in sql and "deleted_at IS NULL" in sql
    assert await live_topic_ids(session, 7, []) == set()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolve_topic_terms_filters_and_merges_keywords_with_entity_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    row = SimpleNamespace(id=uuid4(), keywords=["rates", "fed"], entity_ids=[str(entity_id)])

    async def fake_refs(_s: Any, ids: list[Any]) -> list[Any]:
        return [SimpleNamespace(name="Acme", canonical_id=ids[0])]

    import modules.knowledge.entities.public as entities

    monkeypatch.setattr(entities, "get_entity_refs", fake_refs)
    session = CaptureSession([row])
    result = await resolve_topic_terms(session, 7, [row.id])  # type: ignore[arg-type]
    assert result == {row.id: ["rates", "fed", "Acme"]}
    sql = sql_text(session.statements[0])
    assert "owner_id = 7" in sql and "deleted_at IS NULL" in sql and "is_active IS true" in sql
    assert await resolve_topic_terms(session, 7, []) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deleted_entity_drops_only_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    live, dead = uuid4(), uuid4()
    row = SimpleNamespace(id=uuid4(), keywords=[], entity_ids=[str(live), str(dead)])

    async def fake_refs(_s: Any, ids: list[Any]) -> list[Any]:
        if dead in ids:
            raise LookupError
        return [SimpleNamespace(name="Live Corp", canonical_id=i) for i in ids]

    import modules.knowledge.entities.public as entities

    monkeypatch.setattr(entities, "get_entity_refs", fake_refs)
    result = await resolve_topic_terms(CaptureSession([row]), 7, [row.id])  # type: ignore[arg-type]
    assert result == {row.id: ["Live Corp"]}
    assert [r.name for r in await topics._visible_entity_refs(None, [live, dead])] == ["Live Corp"]  # type: ignore[arg-type]
