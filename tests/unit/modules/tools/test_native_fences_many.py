"""Batched native output fence revalidation matches the per-row verdicts."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.tools.schemas import ToolExecutionPrincipal
from core.workspaces.schemas import WorkspaceContext
from modules.sources import public as sources
from modules.tools import public

_SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
_PRINCIPAL = ToolExecutionPrincipal(
    actor_id="owner:1", scope=_SCOPE, is_owner=True, owner_all_sources=True,
)


@asynccontextmanager
async def _factory() -> Any:
    yield object()


@pytest.mark.asyncio
async def test_many_evaluates_rows_against_one_source_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh, stale = uuid4(), uuid4()
    calls: list[frozenset[Any]] = []

    async def list_tool_sources(_s: Any, **kw: Any) -> Any:
        calls.append(kw["source_ids"])
        items = [SimpleNamespace(id=fresh, generation=3), SimpleNamespace(id=stale, generation=9)]
        return SimpleNamespace(items=[i for i in items if i.id in kw["source_ids"]], next_cursor=None)

    monkeypatch.setattr(sources, "list_tool_sources", list_tool_sources)
    sinks = [
        ({"records": [], "source_generations": {fresh: 3}}, _PRINCIPAL),
        ({"records": [], "source_generations": {stale: 4}}, _PRINCIPAL),  # generation moved on
        ({"records": [], "source_generations": {fresh: 3}}, None),  # unusable principal
        ({"records": "bad", "source_generations": {}}, _PRINCIPAL),  # malformed sink
    ]
    verdicts = await public.revalidate_native_output_fences_many(
        _factory, sinks, destination_kind="remote", multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    assert verdicts == [True, False, False, False]
    assert len(calls) == 1 and calls[0] == frozenset({fresh, stale})
