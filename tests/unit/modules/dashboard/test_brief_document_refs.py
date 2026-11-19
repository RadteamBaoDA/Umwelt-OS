"""``brief_document_refs``: workspace/owner-scoped, bounded, document-only support read."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.dashboard import _brief_reads

SCOPE: Any = SimpleNamespace(workspace_id=uuid4(), actor_id=uuid4())


class _Session:
    """Session whose ``execute`` records the statement and serves the given rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.statement = rows, None

    async def execute(self, statement: Any) -> list[Any]:
        self.statement = statement
        return [SimpleNamespace(_mapping=r) for r in self.rows]


def _row(ref: int) -> dict[str, Any]:
    return {
        "fact_ref": ref,
        "fact_kind": "stories",
        "source_id": uuid4(),
        "document_id": uuid4(),
        "document_version_id": uuid4(),
        "chunk_id": uuid4(),
    }


async def _read(session: _Session, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    async def admit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(_brief_reads, "_admit", admit)
    monkeypatch.setattr(_brief_reads, "_actor", lambda _scope: SCOPE.actor_id)
    return await _brief_reads.brief_document_refs(
        session,  # type: ignore[arg-type]
        uuid4(),
        scope=SCOPE,
        multi_workspace_enabled=False,
    )


@pytest.mark.asyncio
async def test_returns_rows_and_scopes_query(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_row(1), _row(2)])
    out = await _read(session, monkeypatch)
    assert [r.fact_ref for r in out] == [1, 2]
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "daily_briefs.workspace_id" in sql and "daily_briefs.owner_id" in sql
    assert "document_id IS NOT NULL" in sql
    assert "ORDER BY daily_brief_evidence.fact_ref, daily_brief_evidence.support_index" in sql


@pytest.mark.asyncio
async def test_foreign_brief_gives_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _read(_Session([]), monkeypatch) == []


@pytest.mark.asyncio
async def test_query_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([])
    await _read(session, monkeypatch)
    assert session.statement._limit == _brief_reads.MAX_BRIEF_DOCUMENT_REFS == 100
