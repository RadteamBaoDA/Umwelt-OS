"""Characterization of the Memory cleanup page sweeps, export source fence and ``suggest_candidates``."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.errors import ConflictError
from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope
from modules.memory import public
from modules.memory.models import Memory, MemoryCandidate
from modules.memory.public import MemoryService, SourceCopiedEvidenceScope
from modules.sources.schemas import SourceFence

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=2)
FENCE = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=1)
KW: dict[str, Any] = {"scope": OWNER, "multi_workspace_enabled": False}
DOC, VERSION, CHUNK, SOURCE = uuid4(), uuid4(), uuid4(), uuid4()
NOW = datetime(2026, 4, 1, tzinfo=UTC)


class _Session:
    """Queue-driven ``scalars``/``scalar`` results; records adds, flushes and statement order."""

    def __init__(
        self, scalars: list[list[Any]] | None = None, scalar: list[Any] | None = None
    ) -> None:
        self.scalars_q, self.scalar_q = scalars or [], scalar or []
        self.log: list[str] = []
        self.added: list[Any] = []

    async def scalars(self, _stmt: Any) -> SimpleNamespace:
        self.log.append("scalars")
        rows = self.scalars_q.pop(0) if self.scalars_q else []
        return SimpleNamespace(all=lambda: list(rows))

    async def scalar(self, _stmt: Any) -> Any:
        self.log.append("scalar")
        return self.scalar_q.pop(0) if self.scalar_q else None

    async def execute(self, _stmt: Any, *_a: Any, **_k: Any) -> None:
        self.log.append("execute")

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.log.append("flush")


def _memory(
    *, manual: bool = False, provenance: object = None, candidate_id: UUID | None = None
) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=WS,
        actor_user_id=7,
        content="private fact",
        memory_type="fact",
        confidence=0.9,
        reason="r",
        status="active",
        is_manual=manual,
        provenance=provenance if provenance is not None else {},
        candidate_id=candidate_id,
        created_at=NOW,
        updated_at=NOW,
    )


def _candidate(provenance: object = None) -> MemoryCandidate:
    return MemoryCandidate(
        id=uuid4(),
        workspace_id=WS,
        actor_user_id=7,
        content="candidate text",
        memory_type="fact",
        confidence=0.5,
        reason="r",
        status="pending",
        provenance=provenance if provenance is not None else {},
        created_at=NOW,
        updated_at=NOW,
    )


def _doc_evidence() -> DocumentCleanupEvidenceScope:
    return DocumentCleanupEvidenceScope(
        operation_id=uuid4(),
        source_id=SOURCE,
        document_id=DOC,
        references=(),
        next_cursor=None,
        workspace_id=WS,
        actor_user_id=7,
    )


def _doc_prov(**over: object) -> dict[str, object]:
    return {
        "source_id": str(SOURCE),
        "document_id": str(DOC),
        "document_version_id": str(VERSION),
        "chunk_id": str(CHUNK),
        **over,
    }


async def _doc_page(
    session: _Session,
    *,
    cursor: str | None = None,
    limit: int = 100,
    evidence: DocumentCleanupEvidenceScope | None = None,
) -> Any:
    with (
        patch("core.workspaces.public.read_access_fence", AsyncMock(return_value=FENCE)),
        patch.object(public, "bound_cleanup_lock_waits", AsyncMock()),
    ):
        return await public.purge_document_copied_evidence_page(
            session,
            evidence or _doc_evidence(),
            cursor=cursor,
            limit=limit,
            **KW,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_document_page_scrubs_memories_then_candidates_in_one_page() -> None:
    """Manual memories lose only matching document keys; derived and candidate copies are erased."""
    manual = _memory(manual=True, provenance=_doc_prov(origin="manual"))
    linked_candidate = _candidate(_doc_prov())
    derived_via_candidate = _memory(provenance={}, candidate_id=linked_candidate.id)
    derived_own = _memory(provenance=_doc_prov())
    unresolved = _memory(
        provenance={
            "source_id": str(SOURCE),
            "document_id": None,
            "document_version_id": str(uuid4()),
        }
    )
    unrelated = _memory(provenance={"document_id": str(uuid4())})
    cand_match, cand_unresolved = (
        _candidate(_doc_prov()),
        _candidate({"source_id": str(SOURCE), "chunk_id": "bad"}),
    )
    session = _Session(
        scalars=[
            [manual, derived_via_candidate, derived_own, unresolved, unrelated],
            [cand_match.id, cand_unresolved.id],
        ],
        scalar=[linked_candidate, cand_match, cand_unresolved],
    )
    progress = await _doc_page(session)
    assert progress.complete and progress.next_cursor is None and progress.changed
    assert (
        progress.scrubbed_memories,
        progress.scrubbed_candidates,
        progress.unresolved_count,
    ) == (3, 1, 1)
    assert progress.unresolved_reason
    assert manual.provenance == {
        k: v for k, v in _doc_prov(origin="manual").items() if k != "document_id"
    }
    assert manual.status == "active"
    assert derived_via_candidate.status == "forgotten" and derived_via_candidate.content == ""
    assert derived_via_candidate.provenance == {} and derived_own.status == "forgotten"
    assert unresolved.status == "active" and unrelated.status == "active"
    assert cand_match.status == "expired" and cand_match.content == ""
    assert cand_unresolved.status == "pending"
    assert session.log == ["scalars", "scalar", "scalars", "scalar", "scalar"]


@pytest.mark.asyncio
async def test_document_page_cursor_paths() -> None:
    """A full memory page with lookahead pauses mid-memories; a limit-exact page resumes at candidates."""
    evidence = _doc_evidence()
    rows = [_memory(provenance=_doc_prov()) for _ in range(3)]
    session = _Session(scalars=[list(rows)])
    paused = await _doc_page(session, limit=2, evidence=evidence)
    assert paused.complete is False and paused.scrubbed_memories == 2
    kind, after = public._decode_document_memory_cleanup_cursor(paused.next_cursor, evidence)
    assert (kind, after) == ("memories", rows[1].id)
    assert rows[2].status == "active"  # the lookahead row is untouched

    exact = [_memory(provenance={}) for _ in range(2)]
    exact_page = await _doc_page(_Session(scalars=[list(exact)]), limit=2, evidence=evidence)
    assert exact_page.complete is False
    assert public._decode_document_memory_cleanup_cursor(exact_page.next_cursor, evidence) == (
        "candidates",
        None,
    )

    cursor = public._encode_document_memory_cleanup_cursor(evidence, "candidates", None)
    candidates = [_candidate(_doc_prov()), _candidate()]
    more = await _doc_page(
        _Session(scalars=[[c.id for c in candidates]], scalar=[candidates[0]]),
        cursor=cursor,
        limit=1,
        evidence=evidence,
    )
    assert more.complete is False and more.scrubbed_candidates == 1
    assert public._decode_document_memory_cleanup_cursor(more.next_cursor, evidence) == (
        "candidates",
        candidates[0].id,
    )
    gone = await _doc_page(
        _Session(scalars=[[uuid4()]], scalar=[None]), cursor=cursor, evidence=evidence
    )
    assert gone.complete and not gone.changed


def _source_scope() -> tuple[SourceCopiedEvidenceScope, InternalJobScope, SourceFence]:
    evid = SourceCopiedEvidenceScope(operation_id=uuid4(), source_id=SOURCE, generation=3)
    job = InternalJobScope(
        workspace_id=WS,
        actor_user_id=7,
        membership_revision=2,
        source_id=SOURCE,
        source_generation=3,
    )
    return (
        evid,
        job,
        SourceFence(id=SOURCE, workspace_id=WS, status="purging", generation=3, local_only=False),
    )


async def _source_page(
    session: _Session,
    evid: SourceCopiedEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> Any:
    _, job, fence = _source_scope()
    with (
        patch("core.workspaces.public.read_access_fence", AsyncMock(return_value=FENCE)),
        patch.object(public, "bound_cleanup_lock_waits", AsyncMock()),
    ):
        return await public.purge_source_copied_evidence_page_in_uow(
            session,
            evid,
            scope=job,
            multi_workspace_enabled=False,
            access_fence=FENCE,
            source_fence=fence,  # type: ignore[arg-type]
            cursor=cursor,
            limit=limit,
        )


@pytest.mark.asyncio
async def test_source_page_scrubs_by_source_identity() -> None:
    """Manual rows keep content and lose source annotations; derived and linked rows are erased."""
    evid, _, _ = _source_scope()
    manual = _memory(
        manual=True,
        provenance={"source_id": str(SOURCE), "document_id": str(DOC), "origin": "manual"},
    )
    linked = _candidate({"source_id": str(SOURCE)})
    derived_linked = _memory(provenance={}, candidate_id=linked.id)
    derived_own = _memory(provenance={"source_id": str(SOURCE)})
    sweep_candidate = _candidate({"source_id": str(SOURCE)})
    session = _Session(
        scalars=[[manual, derived_linked, derived_own], [sweep_candidate]], scalar=[linked]
    )
    progress = await _source_page(session, evid)
    assert (
        progress.complete
        and progress.changed
        and progress.processed == 4
        and progress.unresolved_count == 0
    )
    assert manual.provenance == {"origin": "manual"} and manual.content == "private fact"
    assert (
        derived_linked.status == "forgotten" and linked.status == "expired" and linked.content == ""
    )
    assert derived_own.status == "forgotten" and sweep_candidate.status == "expired"
    assert session.log == ["scalars", "scalar", "scalars"]


@pytest.mark.asyncio
async def test_source_page_cursor_and_lookahead() -> None:
    """Memory lookahead pauses on the last processed memory; a limit-exact memory page resumes at candidates."""
    evid, _, _ = _source_scope()
    rows = [_memory(provenance={"source_id": str(SOURCE)}) for _ in range(3)]
    paused = await _source_page(_Session(scalars=[list(rows)]), evid, limit=2)
    assert paused.complete is False and paused.processed == 2 and rows[2].status == "active"
    assert public._decode_source_memory_cursor(paused.next_cursor, evid) == ("memories", rows[1].id)
    exact = await _source_page(
        _Session(scalars=[[_memory(provenance={}) for _ in range(2)]]), evid, limit=2
    )
    assert exact.complete is False
    assert public._decode_source_memory_cursor(exact.next_cursor, evid) == ("candidates", None)
    cursor = public._source_memory_cursor(evid, "candidates", None)
    candidates = [_candidate({"source_id": str(SOURCE)}) for _ in range(2)]
    more = await _source_page(_Session(scalars=[list(candidates)]), evid, cursor=cursor, limit=1)
    assert more.complete is False and more.processed == 1
    assert public._decode_source_memory_cursor(more.next_cursor, evid) == (
        "candidates",
        candidates[0].id,
    )
    done = await _source_page(_Session(scalars=[[]]), evid, cursor=cursor)
    assert done.complete and not done.changed and done.next_cursor is None


# --------------------------------------------------------------------------- export source fence


def _export_patches(
    *,
    origin: Any = None,
    refs: Any = None,
    source: Any = None,
    eligible: bool = True,
    purged: bool = False,
) -> list[Any]:
    from modules.chat import public as chat_public
    from modules.knowledge.documents import public as documents_public
    from modules.sources import public as sources_public

    return [
        patch.object(chat_public, "read_memory_export_origin", AsyncMock(return_value=origin)),
        patch.object(documents_public, "read_evidence_refs", AsyncMock(return_value=refs or [])),
        patch.object(sources_public, "get_source_fence", AsyncMock(return_value=source)),
        patch.object(
            sources_public,
            "filter_export_eligible_sources",
            AsyncMock(return_value=[1] if eligible else []),
        ),
        patch.object(sources_public, "source_data_purge_exists", AsyncMock(return_value=purged)),
    ]


async def _fence(row: Any, patches: list[Any]) -> tuple[dict[str, object], Any]:
    from contextlib import ExitStack

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        return await public._memory_export_source_fence(AsyncMock(), row, **KW)


@pytest.mark.asyncio
async def test_export_fence_document_and_chat_evidence_projection() -> None:
    """Verified chat and document evidence produce a fence payload and a safe provenance projection."""
    conversation, message = uuid4(), uuid4()
    origin = SimpleNamespace(
        conversation_id=conversation,
        message_id=message,
        privacy_persisted=True,
        privacy_updated_at=NOW,
        model_dump_json=lambda: "{}",
    )
    ref = SimpleNamespace(document_id=DOC, source_id=SOURCE)
    source = SimpleNamespace(id=SOURCE, generation=3)
    row = _memory(
        provenance={
            "conversation_id": str(conversation),
            "message_id": str(message),
            "document_id": str(DOC),
            "document_version_id": str(VERSION),
            "chunk_id": str(CHUNK),
            "source_id": str(SOURCE),
            "origin": "agent",
        }
    )
    fence, projected = await _fence(row, _export_patches(origin=origin, refs=[ref], source=source))
    assert fence["source_id"] == SOURCE and fence["source_generation"] == 3
    assert fence["document_id"] == DOC and fence["chunk_id"] == CHUNK
    assert fence["conversation_id"] == conversation and fence["chat_privacy_persisted"] is True
    assert fence["chat_evidence_digest"]
    assert (
        projected.origin == "agent"
        and projected.document_id == DOC
        and projected.source_id == SOURCE
    )
    assert projected.conversation_id == conversation


@pytest.mark.asyncio
async def test_export_fence_manual_memory_degrades_and_derived_rejects() -> None:
    """Manual memory survives removed evidence (annotations dropped); derived memory is a 409."""
    ref = SimpleNamespace(document_id=DOC, source_id=SOURCE)
    provenance = {
        "document_id": str(DOC),
        "document_version_id": str(VERSION),
        "chunk_id": str(CHUNK),
        "source_id": str(SOURCE),
    }
    manual = _memory(manual=True, provenance=provenance)
    fence, projected = await _fence(
        manual, _export_patches(refs=[ref], source=None, eligible=False)
    )
    assert (
        fence == {}
        and projected.origin == "manual"
        and projected.source_id is None
        and projected.document_id is None
    )
    for kwargs in (
        {"source": SimpleNamespace(id=SOURCE, generation=3), "eligible": False},
        {"source": SimpleNamespace(id=SOURCE, generation=3), "purged": True},
        {"refs": []},
    ):
        derived = _memory(provenance=provenance)
        patches = _export_patches(**({"refs": [ref], **kwargs} if "refs" not in kwargs else kwargs))
        with pytest.raises(ConflictError):
            await _fence(derived, patches)
    with pytest.raises(ConflictError):
        await _fence(_memory(provenance={}), _export_patches())
    with pytest.raises(ConflictError):
        await _fence(_memory(provenance={"message_id": str(uuid4())}), _export_patches())
    with pytest.raises(ConflictError):
        await _fence(
            _memory(provenance={"conversation_id": str(uuid4()), "message_id": str(uuid4())}),
            _export_patches(origin=None),
        )
    with pytest.raises(ConflictError):
        await _fence(_memory(provenance={"document_id": str(DOC)}), _export_patches())
    with pytest.raises(ConflictError):
        await _fence(
            _memory(provenance={"origin": "weird", "document_id": str(DOC)}), _export_patches()
        )
    # Manual memory with only partial/unknown keys is still exportable.
    fence, projected = await _fence(
        _memory(manual=True, provenance={"message_id": str(uuid4()), "x": 1}), _export_patches()
    )
    assert fence == {} and projected.origin == "manual"


# --------------------------------------------------------------------------- suggest_candidates


@pytest.mark.asyncio
async def test_suggest_candidates_persists_skips_duplicates_and_projects_safely() -> None:
    """Duplicates are skipped, auto-accepted candidates also create a Memory, and output is re-verified."""
    privacy = SimpleNamespace(store_agent_memory=True, auto_accept_memory=True)
    active = [_memory(), _memory()]
    evaluations = iter(
        [
            SimpleNamespace(
                is_duplicate=True,
                confidence_score=0.1,
                novelty_score=0,
                usefulness_score=0,
                reason="dup",
                should_auto_accept=False,
            ),
            SimpleNamespace(
                is_duplicate=False,
                confidence_score=0.9,
                novelty_score=0.8,
                usefulness_score=0.7,
                reason="ok",
                should_auto_accept=True,
            ),
            SimpleNamespace(
                is_duplicate=False,
                confidence_score=0.6,
                novelty_score=0.5,
                usefulness_score=0.4,
                reason="maybe",
                should_auto_accept=False,
            ),
        ]
    )
    session = _Session(scalars=[active])
    verified = AsyncMock(side_effect=[object(), None])
    commit, projected_candidate = (
        AsyncMock(),
        AsyncMock(
            side_effect=lambda _s, row, **_k: (
                SimpleNamespace(id=row.id) if row.reason == "ok" else None
            )
        ),
    )
    service = MemoryService(session, redis=None)  # type: ignore[arg-type]
    conversation, message = uuid4(), uuid4()
    created: list[MemoryCandidate] = []

    async def scalar(_stmt: Any) -> Any:
        session.log.append("scalar")
        return created.pop(0) if created else None

    orig_add = session.add

    def add(row: Any) -> None:
        orig_add(row)
        if isinstance(row, MemoryCandidate):
            row.id = row.id or uuid4()
            created.append(row)

    session.add = add  # type: ignore[method-assign]
    session.scalar = scalar  # type: ignore[method-assign]
    proposals = [{"content": f"c{i}", "type": "fact"} for i in range(3)]
    with (
        patch.object(
            MemoryService,
            "_read_privacy_config_under_fence",
            AsyncMock(return_value=(privacy, FENCE)),
        ) as read_privacy,
        patch.object(public, "extract_candidate_proposals", lambda _t: proposals),
        patch.object(public, "_lock_live_provenance_evidence", AsyncMock()) as lock_evidence,
        patch.object(public, "_verified_memory_read", verified),
        patch.object(public, "evaluate_candidate", lambda *_a, **_k: next(evaluations)),
        patch.object(public, "_to_candidate_read", lambda row: SimpleNamespace(id=row.id)),
        patch.object(public, "commit_with_replay", commit),
        patch.object(MemoryService, "_invalidate_cache", AsyncMock()) as invalidate,
        patch.object(public, "_verified_candidate_read", projected_candidate),
    ):
        result = await service.suggest_candidates(
            "text",
            conversation_id=conversation,
            message_id=message,
            provenance={"x": 1},
            **KW,
        )
    assert read_privacy.await_args.kwargs["lock_fence"] is True
    assert lock_evidence.await_args.args[1] == {
        "x": 1,
        "conversation_id": str(conversation),
        "message_id": str(message),
        "origin": "agent",
    }
    assert lock_evidence.await_args.kwargs["require_copy_evidence"] is True
    candidates = [row for row in session.added if isinstance(row, MemoryCandidate)]
    memories = [row for row in session.added if isinstance(row, Memory)]
    assert [c.content for c in candidates] == ["c1", "c2"]
    assert [c.status for c in candidates] == ["accepted", "pending"]
    assert (
        len(memories) == 1
        and memories[0].candidate_id == candidates[0].id
        and memories[0].is_manual is False
    )
    assert (
        memories[0].status == "active"
        and memories[0].workspace_id == WS
        and memories[0].actor_user_id == 7
    )
    commit.assert_awaited_once()
    invalidate.assert_awaited_once()
    assert len(result) == 1 and result[0].id == candidates[0].id
    assert verified.await_count == 2


@pytest.mark.asyncio
async def test_suggest_candidates_skips_when_disabled_or_empty() -> None:
    """Disabled agent memory or no proposals return an empty list before any evidence lock or write."""
    session = _Session()
    service = MemoryService(session, redis=None)  # type: ignore[arg-type]
    with (
        patch.object(
            MemoryService,
            "_read_privacy_config_under_fence",
            AsyncMock(
                return_value=(
                    SimpleNamespace(store_agent_memory=False, auto_accept_memory=False),
                    FENCE,
                )
            ),
        ),
        patch.object(public, "_lock_live_provenance_evidence", AsyncMock()) as lock_evidence,
    ):
        assert await service.suggest_candidates("t", **KW) == []
    with (
        patch.object(
            MemoryService,
            "_read_privacy_config_under_fence",
            AsyncMock(
                return_value=(
                    SimpleNamespace(store_agent_memory=True, auto_accept_memory=False),
                    FENCE,
                )
            ),
        ),
        patch.object(public, "extract_candidate_proposals", lambda _t: []),
        patch.object(public, "_lock_live_provenance_evidence", AsyncMock()) as lock_evidence,
    ):
        assert await service.suggest_candidates("t", **KW) == []
    lock_evidence.assert_not_awaited()
    assert session.log == []
