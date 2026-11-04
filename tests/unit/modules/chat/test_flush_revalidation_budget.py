"""Token-flush revalidation budget: lock-only checks every flush, full ones at most every N seconds."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.chat import retrieval, worker
from modules.chat.schemas import AnswerContext
from modules.knowledge.documents import _chat_evidence
from modules.sources import public as sources_public
from tests.unit.typing_regressions.test_none_handling_regressions import _evidence

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)


def _ctx(interval: float | None = 2.0) -> Any:
    settings = SimpleNamespace() if interval is None else SimpleNamespace(chat_flush_full_revalidate_seconds=interval)
    c = worker._RunCtx(
        response_id=uuid4(), session_factory=None, settings=settings, redis=None,  # type: ignore[arg-type]
        conversation_id=uuid4(), privacy_fence={}, seq=3, job_scope=SimpleNamespace(),  # type: ignore[arg-type]
    )
    c.answer_context = AnswerContext(query="q", evidence=[_evidence()])
    return c


async def _flush_fence(
    monkeypatch: pytest.MonkeyPatch, c: Any, revalidate: AsyncMock,
) -> worker._FenceOutcome:
    monkeypatch.setattr(worker, "_lock_live_response", AsyncMock(return_value=(True, SimpleNamespace(status="streaming"))))
    monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "_privacy_cancel_locked", AsyncMock())
    monkeypatch.setattr(worker, "revalidate_context_fence", revalidate)
    session = SimpleNamespace(commit=AsyncMock())
    outcome, _run = await worker._fence_live_run(session, c, check_evidence=True, budgeted=True)  # type: ignore[arg-type]
    return outcome


async def test_first_flush_is_full_then_lightweight_until_budget_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _ctx(2.0)
    revalidate = AsyncMock(return_value=(True, []))
    now = asyncio.get_running_loop().time()
    await _flush_fence(monkeypatch, c, revalidate)  # nothing validated yet -> full
    await _flush_fence(monkeypatch, c, revalidate)  # within 2 s -> lock-only
    await _flush_fence(monkeypatch, c, revalidate)
    assert [call.kwargs["lightweight"] for call in revalidate.await_args_list] == [False, True, True]
    assert c.last_full_revalidation is not None and c.last_full_revalidation >= now
    c.last_full_revalidation -= 2.5  # budget elapsed
    await _flush_fence(monkeypatch, c, revalidate)
    assert revalidate.await_args_list[-1].kwargs["lightweight"] is False
    # Every flush still takes the evidence locks (lock_evidence=True) in both modes.
    assert all(call.kwargs["lock_evidence"] is True for call in revalidate.await_args_list)


async def test_revocation_detected_by_the_lightweight_pass_stops_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _ctx(2.0)
    ok = AsyncMock(return_value=(True, []))
    assert await _flush_fence(monkeypatch, c, ok) is worker._FenceOutcome.LIVE  # full pass primes the budget
    revoked = AsyncMock(return_value=(False, ["Evidence source x became local-only before remote publication"]))
    outcome = await _flush_fence(monkeypatch, c, revoked)
    assert revoked.await_args.kwargs["lightweight"] is True  # detected without waiting for the next full pass
    assert outcome is worker._FenceOutcome.REDACTED
    worker._privacy_cancel_locked.assert_awaited()  # type: ignore[attr-defined]


async def test_budget_zero_or_missing_setting_always_runs_the_full_revalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    for interval in (0.0, None):
        c = _ctx(interval)
        revalidate = AsyncMock(return_value=(True, []))
        await _flush_fence(monkeypatch, c, revalidate)
        await _flush_fence(monkeypatch, c, revalidate)
        assert [call.kwargs["lightweight"] for call in revalidate.await_args_list] == [False, False]


async def test_non_flush_fences_are_never_lightweight(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _ctx(2.0)
    revalidate = AsyncMock(return_value=(True, []))
    monkeypatch.setattr(worker, "_lock_live_response", AsyncMock(return_value=(True, SimpleNamespace(status="streaming"))))
    monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "revalidate_context_fence", revalidate)
    c.last_full_revalidation = asyncio.get_running_loop().time()
    await worker._fence_live_run(SimpleNamespace(commit=AsyncMock()), c, check_evidence=True)  # type: ignore[arg-type]
    assert revalidate.await_args.kwargs["lightweight"] is False


async def test_source_fences_are_one_batched_query() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    context = AnswerContext(query="q", evidence=[], fence_snapshot={
        str(first): {"generation": 1, "local_only": False},
        str(second): {"generation": 1, "local_only": False},
        str(third): {"generation": 2, "local_only": False},
        "not-a-uuid": {"generation": 1},
    })

    def fence(source_id: Any, generation: int) -> Any:
        return SimpleNamespace(id=source_id, status="active", generation=generation, local_only=False)

    batch = AsyncMock(return_value={first: fence(first, 1), second: fence(second, 1), third: fence(third, 1)})
    single = AsyncMock()
    with (
        patch.object(retrieval.sources_public, "get_source_fences", batch),
        patch.object(retrieval.sources_public, "get_source_fence", single),
    ):
        reasons = await retrieval._source_fence_reasons(MagicMock(), context, "remote", {"scope": SCOPE})
    batch.assert_awaited_once()
    single.assert_not_awaited()
    assert [r for r in reasons if "generation changed" in r] == [f"Source {third} generation changed (1 != 2)"]
    assert "Invalid source identifier in fence: not-a-uuid" in reasons


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Recorder:
    """Session stub counting statements; get_source_fences returns Source-like rows."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows, self.statements = rows, []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.rows)

    async def scalars(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.rows)


async def test_get_source_fences_reads_many_sources_with_one_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [uuid4() for _ in range(5)]
    rows = [SimpleNamespace(
        id=i, workspace_id=SCOPE.workspace_id, status="active", generation=1, local_only=False,
        graph_enabled=True, rag_index_enabled=True,
    ) for i in ids]
    monkeypatch.setattr(sources_public, "_admit_source_scope", AsyncMock())
    session = _Recorder(rows)
    fences = await sources_public.get_source_fences(session, [*ids, ids[0]], scope=SCOPE, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert len(session.statements) == 1
    assert set(fences) == set(ids) and all(f.generation == 1 for f in fences.values())
    assert await sources_public.get_source_fences(_Recorder([]), [], scope=SCOPE, multi_workspace_enabled=False) == {}  # type: ignore[arg-type]


async def test_lock_only_evidence_state_reads_one_content_free_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    version, chunk, source = uuid4(), uuid4(), uuid4()
    lock_rows = AsyncMock(return_value=True)
    monkeypatch.setattr(_chat_evidence, "_lock_evidence_rows", lock_rows)
    session = _Recorder([(version, chunk, source, 3, "active", False)])
    state = await _chat_evidence.lock_chat_evidence_state(
        session, [(version, chunk)], scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    lock_rows.assert_awaited_once()
    assert len(session.statements) == 1  # one projection; no provenance/ORM row queries after the locks
    sql = str(session.statements[0])
    assert "document_chunks.content" not in sql and "normalized_version_provenance" not in sql
    assert "sources.generation" in sql and "sources.local_only" in sql
    assert [(s.document_version_id, s.chunk_id, s.source_generation, s.local_only) for s in state] == [
        (version, chunk, 3, False)]


async def test_lock_only_evidence_state_matches_the_unlocked_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_chat_evidence, "_lock_evidence_rows", AsyncMock(return_value=False))
    session = _Recorder([])
    assert await _chat_evidence.lock_chat_evidence_state(
        session, [], scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    ) == []
    assert session.statements == []
    monkeypatch.setattr(_chat_evidence, "_lock_evidence_rows", AsyncMock(return_value=True))
    pair = (uuid4(), uuid4())
    with pytest.raises(ValueError):  # exact selection with a missing chunk must fail like the content variant
        await _chat_evidence.lock_chat_evidence_state(
            session, [pair], selection_fences=(), scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
        )


async def _drive_revalidation(monkeypatch: pytest.MonkeyPatch, *, lightweight: bool) -> list[str]:
    """Run revalidate_context_fence over one context; return the ordered owner reads it issued."""
    calls: list[str] = []
    evidence = _evidence()
    context = AnswerContext(query="q", evidence=[evidence])
    context.selection_fences.append(object())  # type: ignore[arg-type]
    state = SimpleNamespace(
        document_version_id=evidence.document_version_id, chunk_id=evidence.chunk_id,
        source_generation=evidence.source_generation, source_status="active", local_only=False,
    )

    def recorder(name: str, result: Any) -> AsyncMock:
        async def run(*_a: Any, **_k: Any) -> Any:
            calls.append(name)
            return result
        return AsyncMock(side_effect=run)

    monkeypatch.setattr(retrieval, "_scope_kwargs", AsyncMock(return_value={"scope": SCOPE}))
    monkeypatch.setattr(retrieval, "_source_fence_reasons", recorder("source_fences", []))
    monkeypatch.setattr(retrieval.documents_public, "validate_gadget_document_selection_fences", recorder("selection", True))
    monkeypatch.setattr(retrieval.documents_public, "lock_chat_evidence_chunks", recorder("lock_chunks_with_content", [state]))
    monkeypatch.setattr(retrieval.documents_public, "lock_chat_evidence_state", recorder("lock_state_only", [state]))
    valid, reasons = await retrieval.revalidate_context_fence(
        MagicMock(), context, lock_evidence=True, lightweight=lightweight, scope=SCOPE,
    )
    assert valid, reasons
    return calls


async def test_lightweight_flush_issues_fewer_reads_than_full_and_no_content_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    full = await _drive_revalidation(monkeypatch, lightweight=False)
    light = await _drive_revalidation(monkeypatch, lightweight=True)
    assert len(light) < len(full)
    assert "lock_chunks_with_content" in full and "selection" in full
    assert light == ["source_fences", "lock_state_only"]  # still locks every flush; no content/provenance read
