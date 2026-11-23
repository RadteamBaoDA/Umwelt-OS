"""Anchored retrieval keeps the record's chunks first, within a half-budget cap, ahead of search."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from modules.chat import retrieval
from modules.chat._worker import setup as worker_setup
from modules.chat.schemas import AnswerContextRequest, EvidenceItem, SelectedEvidenceRef

SCOPE = MagicMock()


def _ev(size: int) -> EvidenceItem:
    """Build an evidence item whose content is ``size`` ASCII bytes (its budget cost)."""
    return EvidenceItem(
        source_id=uuid4(),
        source_type="document",
        source_generation=1,
        local_only=False,
        document_id=uuid4(),
        document_version_id=uuid4(),
        version_number=1,
        chunk_id=uuid4(),
        chunk_index=0,
        content="a" * size,
        title="Title",
        canonical_url=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        published_at=None,
        metadata_is_version_snapshot=False,
        score=0.03,
    )


def _ref(item: EvidenceItem) -> SelectedEvidenceRef:
    """Return the selected ref a client sends for ``item``'s chunk."""
    return SelectedEvidenceRef(
        document_version_id=item.document_version_id,
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        source_id=item.source_id,
    )


def _chunk_for(ref: tuple[UUID, UUID]) -> SimpleNamespace:
    """Build the document-owner chunk shape ``_read_evidence_items`` consumes for ``ref``."""
    version, chunk = ref
    return SimpleNamespace(
        source_id=uuid4(),
        source_generation=1,
        local_only=False,
        document_id=uuid4(),
        document_version_id=version,
        version_number=1,
        chunk_id=chunk,
        chunk_index=0,
        content="x",
        title="T",
        canonical_url=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        published_at=None,
        metadata_is_version_snapshot=False,
    )


def _hit() -> SimpleNamespace:
    """Build one search hit with a random ref and a low RRF-style score."""
    return SimpleNamespace(score=0.03, document_version_id=uuid4(), chunk_id=uuid4())


def _patch_retrieval(search_items: list[Any], read: AsyncMock, rerank: AsyncMock) -> list[Any]:
    """Return the patch context managers that isolate ``build_context`` from I/O."""
    return [
        patch.object(retrieval, "_scope_kwargs", AsyncMock(return_value={})),
        patch.object(
            retrieval.search_public,
            "search",
            AsyncMock(return_value=SimpleNamespace(warnings=[], items=search_items)),
        ),
        patch.object(retrieval.documents_public, "read_chat_evidence_chunks", read),
        patch.object(retrieval, "_resolve_entity_context", AsyncMock(return_value=[])),
        patch.object(retrieval, "_resolve_temporal_context", AsyncMock(return_value=[])),
        patch.object(retrieval, "_apply_configured_reranking", rerank),
    ]


def test_budget_caps_anchor_at_half() -> None:
    """Five 3 000-byte anchor chunks: only one fits the 5 000 half-budget; search fills the rest."""
    anchors = [_ev(3000) for _ in range(5)]
    search = [_ev(1000) for _ in range(5)]
    request = AnswerContextRequest(
        query="q",
        anchored=True,
        selected_refs=[_ref(a) for a in anchors],
        context_budget_bytes=10_000,
    )
    kept, total = retrieval._fit_context_budget(request, anchors + search)
    assert kept == [anchors[0], *search]
    assert total == 8000


def test_budget_keeps_oversized_first_anchor() -> None:
    """An 8 000-byte anchor chunk over the 5 000 half-budget is still kept; search fits the rest."""
    anchor = _ev(8000)
    search = [_ev(1000) for _ in range(3)]
    request = AnswerContextRequest(
        query="q", anchored=True, selected_refs=[_ref(anchor)], context_budget_bytes=10_000
    )
    kept, total = retrieval._fit_context_budget(request, [anchor, *search])
    assert kept == [anchor, search[0], search[1]]
    assert total == 10_000


@pytest.mark.asyncio
async def test_anchor_refs_collected_before_search() -> None:
    """Anchor refs lead the refs read from the owner even when search returns 120 hits."""
    anchor_refs = [(uuid4(), uuid4()), (uuid4(), uuid4())]
    request = AnswerContextRequest(
        query="q",
        anchored=True,
        selected_refs=[
            SelectedEvidenceRef(document_version_id=v, chunk_id=c) for v, c in anchor_refs
        ],
    )
    search = AsyncMock(
        return_value=SimpleNamespace(warnings=[], items=[_hit() for _ in range(120)])
    )
    read = AsyncMock(side_effect=lambda _s, refs, **_k: [_chunk_for(r) for r in refs])
    with (
        patch.object(retrieval, "_scope_kwargs", AsyncMock(return_value={})),
        patch.object(retrieval.search_public, "search", search),
        patch.object(retrieval.documents_public, "read_chat_evidence_chunks", read),
        patch.object(retrieval, "_resolve_entity_context", AsyncMock(return_value=[])),
        patch.object(retrieval, "_resolve_temporal_context", AsyncMock(return_value=[])),
        patch.object(
            retrieval, "_apply_configured_reranking", AsyncMock(return_value=([], "skipped", []))
        ),
    ):
        await retrieval.build_context(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), request, SCOPE
        )
    passed = read.await_args.args[1]
    assert passed[:2] == anchor_refs
    assert search.await_count == 1


@pytest.mark.asyncio
async def test_rerank_only_reorders_search_items() -> None:
    """The reranker receives only search items; the anchor stays first in the returned evidence."""
    anchor_chunk = _chunk_for((uuid4(), uuid4()))
    anchor_ref = (anchor_chunk.document_version_id, anchor_chunk.chunk_id)
    request = AnswerContextRequest(
        query="q",
        anchored=True,
        selected_refs=[
            SelectedEvidenceRef(document_version_id=anchor_ref[0], chunk_id=anchor_ref[1])
        ],
    )
    read = AsyncMock(side_effect=lambda _s, refs, **_k: [_chunk_for(r) for r in refs])
    rerank = AsyncMock(side_effect=lambda *args, **_kw: (list(reversed(args[5])), "applied", []))
    with ExitStack() as stack:
        for patcher in _patch_retrieval([_hit(), _hit()], read, rerank):
            stack.enter_context(patcher)
        ctx = await retrieval.build_context(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), request, SCOPE
        )
    reranked_input = rerank.await_args.args[5]
    assert all((item.document_version_id, item.chunk_id) != anchor_ref for item in reranked_input)
    assert (ctx.evidence[0].document_version_id, ctx.evidence[0].chunk_id) == anchor_ref
    assert len(ctx.evidence) == 3


def test_exact_selection_budget_unchanged() -> None:
    """An exact selection whose evidence overflows the budget still raises ValueError."""
    request = AnswerContextRequest(query="q", selected_only=True, context_budget_bytes=1000)
    with pytest.raises(ValueError, match="exceeds the bounded chat context budget"):
        retrieval._fit_context_budget(request, [_ev(800), _ev(800)])


@pytest.mark.asyncio
async def test_setup_passes_anchored() -> None:
    """The worker request built from a stored anchored context carries ``anchored=True``."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.scalar = AsyncMock(
        side_effect=[SimpleNamespace(id=uuid4(), content="hi", revision_of_message_id=None), None]
    )
    anchor_ref = {
        "document_version_id": str(uuid4()),
        "chunk_id": str(uuid4()),
        "document_id": str(uuid4()),
        "source_id": str(uuid4()),
    }
    c: Any = SimpleNamespace(
        response_id=uuid4(),
        privacy_fence=None,
        seq=1,
        job_scope=None,
        access_fence=None,
        scope=None,
        redis=None,
    )
    claimed = SimpleNamespace(
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        context_req={
            "source_scope": [],
            "anchored": True,
            "selected_only": False,
            "selected_refs": [anchor_ref],
            "selection_fences": [],
        },
    )
    with (
        patch.object(worker_setup, "_lock_live_response", AsyncMock(return_value=(False, None))),
        patch.object(worker_setup, "is_run_cancelled", AsyncMock(return_value=False)),
        patch.object(worker_setup, "_history_cutoff", AsyncMock(return_value=datetime.now(UTC))),
        patch.object(worker_setup, "multi_workspace_enabled", return_value=False),
    ):
        turn = await worker_setup._load_turn_inputs(session, c, claimed)
    assert turn.answer_request.anchored is True
