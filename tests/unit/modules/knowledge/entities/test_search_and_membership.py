"""Entity search LIKE escaping and single-round-trip extraction membership recording."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.entities import public

_SCOPE = SimpleNamespace(workspace_id=uuid4())


async def _admit(*_a: Any, **_k: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_list_entities_escapes_like_wildcards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public, "_admit", _admit)

    async def aliases(*_a: Any, **_k: Any) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(public, "_aliases", aliases)
    seen: list[Any] = []

    class _Session:
        async def scalars(self, stmt: Any) -> Any:
            seen.append(stmt)
            return SimpleNamespace(all=list)

    await public.list_entities(
        _Session(), 10, None, None, r"50%_a\b", scope=_SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    compiled = seen[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ESCAPE" in sql
    params = [v for v in compiled.params.values() if isinstance(v, str)]
    assert any(r"50\%\_a" in v for v in params), params


def _args(entity_id: Any) -> dict[str, Any]:
    ref = SimpleNamespace(source_generation=1, document_id=uuid4(), source_id=uuid4(),
                          document_version_id=uuid4(), chunk_id=uuid4())
    from datetime import UTC, datetime
    return {
        "entity_id": entity_id, "evidence_ref": ref, "source_generation": 1, "extraction_identity": "x",
        "candidate_key": "k", "match_fingerprint": "f" * 64, "observed_at": datetime.now(UTC),
        "confidence": 0.5, "scope": _SCOPE, "multi_workspace_enabled": False,
    }


class _Session:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.executes = 0

    async def scalar(self, *_a: Any) -> Any:
        return SimpleNamespace()

    async def execute(self, *_a: Any) -> Any:
        self.executes += 1
        row = self.results.pop(0)
        return SimpleNamespace(first=lambda: row)


@pytest.mark.asyncio
async def test_membership_fresh_insert_is_one_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public, "_admit", _admit)
    entity_id, mid = uuid4(), uuid4()
    session = _Session([(mid, entity_id)])
    assert await public.record_extraction_membership(session, **_args(entity_id)) == mid  # type: ignore[arg-type]
    assert session.executes == 1


@pytest.mark.asyncio
async def test_membership_conflict_reads_back_and_checks_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public, "_admit", _admit)
    entity_id, mid = uuid4(), uuid4()
    session = _Session([None, (mid, entity_id)])
    assert await public.record_extraction_membership(session, **_args(entity_id)) == mid  # type: ignore[arg-type]
    assert session.executes == 2
    with pytest.raises(ValueError):
        await public.record_extraction_membership(_Session([None, (mid, uuid4())]), **_args(entity_id))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await public.record_extraction_membership(_Session([None, None]), **_args(entity_id))  # type: ignore[arg-type]
