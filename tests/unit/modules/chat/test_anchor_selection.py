"""Anchored single-record selections resolve with plain-chat meaning, capped, and keep local-only refusal."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.errors import ConflictError, DomainError
from modules.chat._public import conversations
from modules.chat._worker.base import _pin_task_thread_scope
from modules.chat.public import reject_unsendable_selection, resolve_gadget_context

_DOC = "modules.knowledge.documents.public"


def _projection(*, chunk_count: int, truncated: bool, local_only: bool = False) -> SimpleNamespace:
    """Build a minimal News document projection stand-in with ``chunk_count`` chunks.

    ``ids`` keeps the chunk ids in projection order so tests can assert the leading slice.
    """
    ids = [uuid4() for _ in range(chunk_count)]
    return SimpleNamespace(
        document_id=uuid4(),
        document_version_id=uuid4(),
        source_id=uuid4(),
        current_source_generation=1,
        source_type="rss",
        provider=None,
        local_only=local_only,
        scope_discriminator=None,
        chunks=[SimpleNamespace(id=cid) for cid in ids],
        chunks_truncated=truncated,
        ids=ids,
    )


def _item(p: SimpleNamespace) -> dict[str, str]:
    """Return the camelCase selection item the dashboard sends for projection ``p``."""
    return {
        "sourceId": str(p.source_id),
        "documentId": str(p.document_id),
        "documentVersionId": str(p.document_version_id),
    }


async def _resolve(projection: SimpleNamespace, context: dict[str, Any]) -> dict[str, Any]:
    """Run ``resolve_gadget_context`` with the Documents reads patched to return ``projection``."""
    with (
        patch(f"{_DOC}.get_news_document_projection", AsyncMock(return_value=projection)),
        patch(f"{_DOC}.validate_gadget_document_selection_fences", AsyncMock(return_value=True)),
    ):
        return await resolve_gadget_context(
            AsyncMock(),
            context,
            scope=None,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
        )


async def _code(projection: SimpleNamespace, context: dict[str, Any]) -> tuple[int, str]:
    """Return (status, code) of the DomainError raised resolving ``context`` with patched reads."""
    with pytest.raises(DomainError) as caught:
        await _resolve(projection, context)
    return caught.value.status_code, caught.value.detail["code"]


@pytest.mark.asyncio
async def test_anchor_single_item_resolves_unfenced() -> None:
    """One anchor item resolves to plain-chat meaning: no source filter, server-derived marker."""
    p = _projection(chunk_count=3, truncated=False)
    result = await _resolve(p, {"kind": "selection", "anchor": True, "items": [_item(p)]})
    assert result["selected_only"] is False
    assert result["anchored"] is True
    assert result["source_scope"] == []
    assert len(result["selected_refs"]) == 3
    assert len(result["selection_fences"]) == 1


@pytest.mark.asyncio
async def test_anchor_rejects_multiple_items() -> None:
    """Two items with ``anchor: true`` fail with the anchor-specific 422 code."""
    p1 = _projection(chunk_count=1, truncated=False)
    p2 = _projection(chunk_count=1, truncated=False)
    context = {"kind": "selection", "anchor": True, "items": [_item(p1), _item(p2)]}
    assert await _code(p1, context) == (422, "anchor_requires_single_item")


@pytest.mark.asyncio
async def test_anchor_truncated_document_uses_leading_chunks() -> None:
    """A truncated 30-chunk projection yields exactly the first 20 chunk ids, in order."""
    p = _projection(chunk_count=30, truncated=True)
    result = await _resolve(p, {"kind": "selection", "anchor": True, "items": [_item(p)]})
    assert [ref["chunk_id"] for ref in result["selected_refs"]] == [
        str(cid) for cid in p.ids[: conversations.ANCHOR_MAX_CHUNKS]
    ]
    assert len(result["selected_refs"]) == 20


@pytest.mark.asyncio
async def test_exact_selection_truncated_still_422() -> None:
    """Without ``anchor`` the same truncated projection keeps the exact-selection 422."""
    p = _projection(chunk_count=30, truncated=True)
    assert await _code(p, {"kind": "selection", "items": [_item(p)]}) == (
        422,
        "selection_too_large",
    )


@pytest.mark.asyncio
async def test_client_anchored_key_stripped() -> None:
    """Client-sent ``anchored``/``selected_only`` never survive; a non-anchor selection stays exact."""
    non_selection = await resolve_gadget_context(
        AsyncMock(),
        {"kind": "document", "anchored": True, "selected_only": True},
        scope=None,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )
    assert "anchored" not in non_selection
    assert "selected_only" not in non_selection

    p = _projection(chunk_count=2, truncated=False)
    selection = await _resolve(p, {"kind": "selection", "items": [_item(p)], "anchored": True})
    assert "anchored" not in selection
    assert selection["selected_only"] is True


@pytest.mark.asyncio
async def test_anchor_local_only_still_rejected() -> None:
    """An anchor's fence marked local_only is refused by ``reject_unsendable_selection``."""
    p = _projection(chunk_count=2, truncated=False, local_only=True)
    resolved = await _resolve(p, {"kind": "selection", "anchor": True, "items": [_item(p)]})
    with patch("modules.sources.public.get_source", AsyncMock(return_value=None)):
        with pytest.raises(ConflictError) as caught:
            await reject_unsendable_selection(
                AsyncMock(),
                resolved,
                scope=None,  # type: ignore[arg-type]
                multi_workspace_enabled=False,
            )
    assert caught.value.detail["code"] == "selection_local_only"


def test_task_thread_drops_anchored() -> None:
    """A scheduled-task thread removes a client-supplied ``anchored`` marker."""
    context: dict[str, Any] = {"anchored": True, "selected_only": True}
    assert _pin_task_thread_scope(
        context, {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": []}
    )
    assert "anchored" not in context
