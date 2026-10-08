"""D2a-2 owner cleanup seams: workspace-qualified discovery, ordered prepare locks, held apply."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.ingestion import public as ingestion
from modules.knowledge.documents.schemas import DocumentCleanupPreparationLimitError
from modules.knowledge.entities import public as entities
from modules.knowledge.observations import public as observations
from modules.knowledge.relationships import public as relationships
from modules.knowledge.temporal import public as temporal
from modules.sources.schemas import SourceFence
from modules.timeline import public as timeline

WS, SRC, DOC = uuid4(), uuid4(), uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=2)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=2)
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=2, configuration_revision=5)
SRC_FENCE = SourceFence(id=SRC, workspace_id=WS, status="purging", generation=1, local_only=False)
KW = {"scope": SCOPE, "multi_workspace_enabled": False}
HELD = {**KW, "access_fence": FENCE, "source_fence": SRC_FENCE}
M1, M2 = sorted(uuid4() for _ in range(2))


class _Result:
    def __init__(self, rows: object) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return list(self.rows)  # type: ignore[call-overload]


class Rec:
    """Recording AsyncSession double; queued results are consumed in statement order."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.stmts: list[object] = []
        self.get_kwargs: list[dict[str, object]] = []

    def _next(self, stmt: object) -> object:
        self.stmts.append(stmt)
        return self.results.pop(0) if self.results else []

    async def execute(self, stmt: object) -> _Result:
        return _Result(self._next(stmt))

    async def scalars(self, stmt: object) -> _Result:
        return _Result(self._next(stmt))

    async def scalar(self, stmt: object) -> object:
        self._next(stmt)
        return None

    async def get(self, model: object, ident: object, **kwargs: object) -> None:
        self.get_kwargs.append(kwargs)

    async def delete(self, obj: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    def texts(self) -> list[str]:
        return [str(s.compile(dialect=postgresql.dialect())) for s in self.stmts]  # type: ignore[attr-defined]

    def params(self) -> list[object]:
        values: list[object] = []
        for s in self.stmts:
            values.extend(s.compile(dialect=postgresql.dialect()).params.values())  # type: ignore[attr-defined]
        return values


def _flat(values: list[object]) -> list[object]:
    return [x for v in values for x in (v if isinstance(v, list) else [v])]


def _tables(rec: Rec) -> list[str]:
    import re
    return [m.group(1) for text in rec.texts() if (m := re.search(r"FROM (\w+)", text))]


_REAL_ADMIT = {m: m._admit for m in (entities, relationships, timeline, temporal, observations)}


@pytest.fixture(autouse=True)
def _admit():
    with patch.object(entities, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(relationships, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(timeline, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(temporal, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(observations, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(ingestion, "_admit_ingestion_scope", AsyncMock(return_value=FENCE)):
        yield


# ---------- owner closures built from discovery results ----------

ENT_RESULTS = lambda: (
    [(M1, M2)], [(M1, M1)], [M2], [(M1, M2)],
)


async def _entity_closure(results=None, document_id=None):
    return await entities.support_cleanup_ids(
        Rec(*(results or ENT_RESULTS())), source_id=SRC, document_id=document_id, **KW)


async def _relationship_closure(results=None, refs=(), document_id=None):
    return await relationships.support_cleanup_ids(
        Rec(*(results or [[(M1, M2)], [(M1, M2)]])), refs=list(refs), source_id=SRC,
        document_id=document_id, membership_ids=[M1], **KW)


TL_RESULTS = lambda: ([(M1, M2)], [M1], [M2])


async def _timeline_closure(results=None, document_id=None):
    return await timeline.support_cleanup_ids(
        Rec(*(results or TL_RESULTS())), source_id=SRC, document_id=document_id, **KW)


async def _temporal_closure(results=None, document_id=None):
    return await temporal.tombstone_cleanup_ids(
        Rec(*(results or ([M1], [(M1, M2, M2)], [M2]))), source_id=SRC, document_id=document_id, **KW)


async def _observation_closure(document_id=None):
    return await observations.observation_cleanup_ids(
        Rec([M1, M2]), source_id=SRC, document_id=document_id, **KW)


# ---------- discovery ----------

@pytest.mark.asyncio
async def test_discovery_sql_is_workspace_qualified() -> None:
    cases = [
        (lambda r: entities.support_cleanup_ids(r, source_id=SRC, **KW), ENT_RESULTS()),
        (lambda r: relationships.support_cleanup_ids(
            r, refs=[(M1, M2)], source_id=SRC, membership_ids=[M1], **KW), ([(M1, M2)], [(M1, M2)], [7], [])),
        (lambda r: timeline.support_cleanup_ids(r, source_id=SRC, **KW), TL_RESULTS()),
        (lambda r: temporal.tombstone_cleanup_ids(r, source_id=SRC, **KW), ([M1], [(M1, M2, M2)], [M2])),
        (lambda r: observations.observation_cleanup_ids(r, source_id=SRC, **KW), ([M1],)),
    ]
    for build, results in cases:
        rec = Rec(*results)
        await build(rec)
        assert len(rec.stmts) >= 1 and all("workspace_id" in text for text in rec.texts())
        assert WS in _flat(rec.params())


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [entities, relationships, timeline, temporal, observations])
async def test_member_denied_before_any_query(module) -> None:
    rec = Rec()
    with pytest.raises(HTTPException) as caught:
        await _REAL_ADMIT[module](rec, scope=MEMBER, multi_workspace_enabled=False)
    assert caught.value.status_code == 403 and rec.stmts == []


@pytest.mark.asyncio
async def test_overflow_flag_without_raising() -> None:
    big = lambda n: [(uuid4(), uuid4()) for _ in range(n)]
    assert (await entities.support_cleanup_ids(Rec(big(10_001)), source_id=SRC, **KW)).overflow
    assert (await relationships.support_cleanup_ids(
        Rec(big(10_001)), refs=[], source_id=SRC, membership_ids=[], **KW)).overflow
    assert (await timeline.support_cleanup_ids(Rec(big(10_001)), source_id=SRC, **KW)).overflow
    assert (await temporal.tombstone_cleanup_ids(
        Rec([uuid4() for _ in range(10_001)]), source_id=SRC, **KW)).overflow
    assert (await observations.observation_cleanup_ids(
        Rec([uuid4() for _ in range(10_001)]), source_id=SRC, **KW)).overflow


@pytest.mark.asyncio
async def test_relationship_history_scan_ceiling_sets_overflow() -> None:
    pages = [list(range(i * 100 + 1, i * 100 + 101)) for i in range(101)]
    closure = await relationships.support_cleanup_ids(
        Rec([], *pages), refs=[(M1, M2)], source_id=SRC, membership_ids=[], **KW)
    assert closure.overflow and len(closure.history_ids) == 10_000


# ---------- prepare ----------

@pytest.mark.asyncio
async def test_entities_prepare_locks_in_order() -> None:
    closure = await _entity_closure()
    rec = Rec()
    other = uuid4()
    await entities.prepare_support_cleanup_in_uow(rec, closure, entity_ids=(other,), **HELD)
    assert all("FOR UPDATE" in text for text in rec.texts())
    assert _tables(rec) == [
        "entities", "entity_evidence_memberships", "entity_aliases",
        "entity_alias_evidence", "entity_field_evidence",
    ]
    assert other in _flat(rec.params())


@pytest.mark.asyncio
async def test_relationships_timeline_temporal_observations_prepare_in_order() -> None:
    rec = Rec()
    await relationships.prepare_support_cleanup_in_uow(
        rec, relationships.RelationshipSupportClosure(SRC, None, (M1,), (M1,), (M2,), (), (3,), False),
        **HELD)
    assert _tables(rec) == ["relationships", "relationship_evidence", "relationship_snapshot_history"]
    rec = Rec()
    await timeline.prepare_support_cleanup_in_uow(rec, await _timeline_closure(), **HELD)
    assert _tables(rec) == ["timeline_events", "timeline_event_evidence", "timeline_event_participants"]
    rec = Rec()
    await temporal.prepare_tombstone_scope_in_uow(rec, await _temporal_closure(), **HELD)
    assert _tables(rec) == ["temporal_mappings", "temporal_supports", "temporal_operations"]
    rec = Rec()
    await observations.prepare_document_cleanup_in_uow(rec, await _observation_closure(), **HELD)
    assert _tables(rec) == ["observations"]
    assert all("FOR UPDATE" in text for text in rec.texts())


@pytest.mark.asyncio
async def test_prepare_rejects_overflowed_closure_and_stale_fence() -> None:
    closure = await timeline.support_cleanup_ids(
        Rec([(uuid4(), uuid4()) for _ in range(10_001)]), source_id=SRC, **KW)
    with pytest.raises(ValueError):
        await timeline.prepare_support_cleanup_in_uow(Rec(), closure, **HELD)
    stale = {**HELD, "access_fence": AccessFence(WS, 1, 2, 6)}
    with pytest.raises(HTTPException) as caught:
        await observations.prepare_document_cleanup_in_uow(Rec(), await _observation_closure(), **stale)
    assert caught.value.status_code == 409
    wrong_source = {**HELD, "source_fence": SRC_FENCE.model_copy(update={"id": uuid4()})}
    with pytest.raises(HTTPException):
        await temporal.prepare_tombstone_scope_in_uow(Rec(), await _temporal_closure(), **wrong_source)


@pytest.mark.asyncio
async def test_ingestion_prepare_locks_and_limit() -> None:
    rec = Rec([uuid4(), uuid4()])
    await ingestion.prepare_document_materializations_in_uow(rec, DOC, source_id=SRC, **HELD)
    assert "FOR UPDATE" in rec.texts()[0] and "workspace_id" in rec.texts()[0]
    with pytest.raises(DocumentCleanupPreparationLimitError) as caught:
        await ingestion.prepare_document_materializations_in_uow(
            Rec([uuid4() for _ in range(10_001)]), DOC, source_id=SRC, **HELD)
    assert str(caught.value) == "ingestion"


# ---------- held apply ----------

def _no_locks(rec: Rec) -> None:
    assert not any("FOR UPDATE" in text for text in rec.texts())
    assert not any(kwargs.get("with_for_update") for kwargs in rec.get_kwargs)


@pytest.mark.asyncio
async def test_entities_held_apply_no_locks_and_detects_change() -> None:
    closure = await _entity_closure()
    rec = Rec(*ENT_RESULTS())
    assert await entities.remove_source_support(rec, closure, **HELD) == 1
    _no_locks(rec)
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await entities.remove_source_support(Rec([(M2, M2)], [], [], []), closure, **HELD)
    with pytest.raises(ValueError):
        await entities.remove_document_support(Rec(*ENT_RESULTS()), closure, **HELD)


@pytest.mark.asyncio
async def test_relationships_held_apply_no_locks_and_detects_change() -> None:
    results = [[(M1, M2)], [(M1, M2)]]
    closure = await _relationship_closure(results)
    rec = Rec(*results)
    assert await relationships.remove_source_support(
        rec, closure, refs=[], membership_ids=[M1], **HELD) == 1
    _no_locks(rec)
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await relationships.remove_source_support(
            Rec([(M2, M2)], []), closure, refs=[], membership_ids=[M1], **HELD)


@pytest.mark.asyncio
async def test_relationships_history_purge_is_unlocked_and_detects_change() -> None:
    refs = [(M1, M2)]
    results = [[(M1, M2)], [(M1, M2)], [7], []]
    closure = await _relationship_closure(results, refs=refs)
    assert closure.history_ids == (7,)
    rec = Rec(*results, [])
    await relationships.purge_history_support(rec, closure, refs, **HELD)
    _no_locks(rec)
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await relationships.purge_history_support(Rec([], [], [], []), closure, refs, **HELD)


@pytest.mark.asyncio
async def test_timeline_held_apply_returns_scoped_collection_change() -> None:
    closure = await _timeline_closure()
    rec = Rec(*TL_RESULTS())
    drafts = await timeline.remove_source_support(rec, closure, **HELD)
    _no_locks(rec)
    assert len(drafts) == 1
    assert drafts[0].scope == "timeline_collection" and drafts[0].source_id == SRC
    assert drafts[0].principal_workspace_id == WS
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await timeline.remove_source_support(Rec([], [], []), closure, **HELD)
    empty = await timeline.support_cleanup_ids(Rec(), source_id=SRC, **KW)
    assert await timeline.remove_source_support(Rec(), empty, **HELD) == []


@pytest.mark.asyncio
async def test_temporal_held_apply_tombstones_without_locks() -> None:
    closure = await _temporal_closure()
    mapping = SimpleNamespace(
        id=M1, desired_revision=1, tombstoned=False, status="applied", canonical_state={"a": 1},
        desired_digest="x",
    )
    support = SimpleNamespace(removed=False)
    rec = Rec([M1], [(M1, M2, M2)], [M2], [mapping], [support])
    with patch.object(temporal, "_queue", AsyncMock()) as queue:
        await temporal.tombstone_scope_in_uow(rec, closure, **HELD)
    _no_locks(rec)
    assert mapping.tombstoned and mapping.status == "tombstoned" and mapping.desired_revision == 2
    assert mapping.canonical_state == {} and support.removed
    queue.assert_awaited_once_with(rec, mapping, "delete")
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await temporal.tombstone_scope_in_uow(Rec([], [], []), closure, **HELD)
    assert not hasattr(temporal, "tombstone_scope")


@pytest.mark.asyncio
async def test_observations_held_apply_modes() -> None:
    closure = await _observation_closure()
    rec = Rec([M1, M2])
    await observations.purge_source_in_uow(rec, SRC, closure=closure, **HELD)
    _no_locks(rec)
    assert "UPDATE observations" in rec.texts()[1] and "DELETE FROM observations" in rec.texts()[2]
    doc_closure = await _observation_closure(DOC)
    rec = Rec([M1, M2])
    await observations.purge_document_in_uow(rec, DOC, source_id=SRC, closure=doc_closure, **HELD)
    _no_locks(rec)
    assert "UPDATE" not in rec.texts()[1] and "DELETE FROM observations" in rec.texts()[1]
    with pytest.raises(RuntimeError, match="cleanup closure changed"):
        await observations.purge_source_in_uow(Rec([M1]), SRC, closure=closure, **HELD)
    with pytest.raises(ValueError):
        await observations.purge_source_in_uow(Rec([M1, M2]), SRC, closure=doc_closure, **HELD)
