"""KnowledgeService forwards the bound scope and flag to every owner callee."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.workspaces import public as workspaces
from modules.knowledge import service as svc
from modules.knowledge.service import KnowledgeService

SCOPE = object()
SESSION = object()


def _record(monkeypatch, module, name, calls, result=None):
    async def fake(*args, **kwargs):
        calls.append((name, args, kwargs))
        return result

    monkeypatch.setattr(module, name, fake, raising=False)


def _facade(flag=True):
    return KnowledgeService(SESSION, scope=SCOPE, multi_workspace_enabled=flag)  # type: ignore[arg-type]


def _assert_bound(calls, flag=True):
    assert calls
    for name, _args, kwargs in calls:
        assert kwargs["scope"] is SCOPE, name
        assert kwargs["multi_workspace_enabled"] is flag, name


def test_flag_must_be_bool():
    with pytest.raises(TypeError):
        KnowledgeService(SESSION, scope=SCOPE, multi_workspace_enabled=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("flag", [True, False])
async def test_simple_methods_forward_scope(monkeypatch, flag):
    calls: list = []
    for mod, names in (
        (svc.entities, ["list_entities", "get_entity", "list_entity_evidence", "list_review_candidates",
                        "assign_review_candidate", "resolve_relationship_review", "list_entity_history"]),
        (svc.relationships, ["get_neighbors"]),
        (svc.timeline, ["list_events", "list_timeline"]),
        (svc.temporal, ["find_changes"]),
        (svc.documents, ["read_chat_evidence_chunks"]),
    ):
        for n in names:
            _record(monkeypatch, mod, n, calls)
    f, eid = _facade(flag), uuid4()
    await f.entities(limit=1, cursor=None, entity_type=None, query=None)
    await f.entity(eid)
    await f.entity_evidence(eid, limit=1, cursor=None)
    await f.entity_neighbors(eid, limit=1, cursor=None)
    await f.entity_review(limit=1)
    await f.assign_review_candidate(eid, None, actor_id=1)  # type: ignore[arg-type]
    await f.resolve_relationship_review(eid, None, actor_id=1)  # type: ignore[arg-type]
    await f.get_events()
    await f.get_timeline(None)  # type: ignore[arg-type]
    await f.entity_history(eid)
    await f.find_changes(kind="x")
    await f.chat_evidence([])
    assert len(calls) == 12
    _assert_bound(calls, flag)


async def test_memory_and_chat_seams_forward_scope(monkeypatch):
    import modules.memory.public as memory
    from modules.chat import retrieval

    calls: list = []
    _record(monkeypatch, retrieval, "build_context", calls)

    class Memory:
        def __init__(self, session):
            pass

        async def get_memories(self, **kw):
            calls.append(("get_memories", (), kw))

        async def get_active_memory_context(self, **kw):
            calls.append(("get_active_memory_context", (), kw))

    monkeypatch.setattr(memory, "MemoryService", Memory)
    f = _facade()
    await f.build_answer_context(None, None, None, None)  # type: ignore[arg-type]
    await f.get_memories()
    await f.get_memory_context()
    assert len(calls) == 3
    _assert_bound(calls)


async def test_entity_timeline_batches_carry_scope(monkeypatch):
    calls: list = []
    ids = [uuid4() for _ in range(101)]
    _record(monkeypatch, svc.entities, "resolve_canonical_entity_id", calls, result=uuid4())
    _record(monkeypatch, svc.timeline, "list_timeline", calls,
            result=SimpleNamespace(items=[SimpleNamespace(evidence=[{"document_version_id": str(i)} for i in ids])]))
    _record(monkeypatch, svc.temporal, "mapping_statuses", calls, result=[])
    query = SimpleNamespace(model_copy=lambda update: query)
    await _facade().get_entity_timeline(uuid4(), query, graph_enabled=True)  # type: ignore[arg-type]
    batches = [c for c in calls if c[0] == "mapping_statuses"]
    assert [len(b[1][1]) for b in batches] == [100, 1]
    _assert_bound(calls)


@pytest.mark.parametrize("key", ["scope", "multi_workspace_enabled"])
async def test_find_changes_rejects_overrides(key):
    with pytest.raises(TypeError):
        await _facade().find_changes(**{key: True})


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": True}, {"after": "x"}])
async def test_candidate_ids_validate(kwargs):
    with pytest.raises(ValueError):
        await workspaces.list_workspace_job_candidate_ids(None, **kwargs)  # type: ignore[arg-type]


async def test_candidate_ids_sql():
    seen = []

    class Session:
        async def scalars(self, query):
            seen.append(query)
            return []

    assert await workspaces.list_workspace_job_candidate_ids(Session(), after=UUID(int=1)) == ()  # type: ignore[arg-type]
    sql = str(seen[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY workspaces.id" in sql and "LIMIT" in sql and "workspaces.id >" in sql
    assert "DISTINCT" not in sql
