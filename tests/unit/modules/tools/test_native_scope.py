"""Workspace-scope contracts for native tool handlers and output-fence revalidation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.tools.schemas import ToolExecutionPrincipal, ToolOutputFence
from core.workspaces.schemas import WorkspaceContext
from modules.tools import builtins
from modules.tools.public import purge_browser_results_in_uow, revalidate_native_output_fences

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=1)
FLAG = False


def _principal() -> ToolExecutionPrincipal:
    return ToolExecutionPrincipal(
        actor_id="owner:7", scope=OWNER, is_owner=True, owner_all_sources=True,
        destinations=frozenset({"local"}), capabilities=frozenset({"source.read"}),
    )


@asynccontextmanager
async def _session_cm() -> Any:
    yield object()


def _remote_case() -> tuple[dict[str, Any], str]:
    source_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    fence = ToolOutputFence(
        document_id=document_id, document_version_id=version_id, source_id=source_id, source_generation=1,
    )
    return {"records": [fence], "source_generations": {source_id: 1}}, str(source_id)


def _sources_page(sink: dict[str, Any]) -> Any:
    (source_id,) = sink["source_generations"]
    return SimpleNamespace(items=[SimpleNamespace(id=source_id, generation=1)], next_cursor=None)


@pytest.mark.asyncio
async def test_document_only_fences_revalidate_with_scope_keywords() -> None:
    """Fakes REQUIRE scope/flag keywords: before T-F1 the missing scope was swallowed as 'suppress'."""
    sink, _ = _remote_case()

    async def list_sources(session: object, *, scope: Any, multi_workspace_enabled: bool, **_k: object) -> Any:
        assert scope == OWNER and multi_workspace_enabled is FLAG
        return _sources_page(sink)

    async def revalidate(session: object, fences: object, *, scope: Any, multi_workspace_enabled: bool, **_k: object) -> bool:
        assert scope == OWNER and multi_workspace_enabled is FLAG
        return True

    with (
        patch("modules.sources.public.list_tool_sources", list_sources),
        patch("modules.knowledge.documents.public.revalidate_tool_document_fences", revalidate),
    ):
        assert await revalidate_native_output_fences(
            _session_cm, sink, _principal(), destination_kind="remote", multi_workspace_enabled=FLAG,
        ) is True


@pytest.mark.asyncio
async def test_type_errors_propagate_and_non_bool_flag_fails_closed() -> None:
    sink, _ = _remote_case()
    boom = AsyncMock(side_effect=TypeError("missing scope"))
    with patch("modules.sources.public.list_tool_sources", boom), pytest.raises(TypeError):
        await revalidate_native_output_fences(
            _session_cm, sink, _principal(), destination_kind="remote", multi_workspace_enabled=FLAG,
        )
    denied = AsyncMock(side_effect=HTTPException(status_code=403))
    with patch("modules.sources.public.list_tool_sources", denied):
        assert await revalidate_native_output_fences(
            _session_cm, sink, _principal(), destination_kind="remote", multi_workspace_enabled=FLAG,
        ) is False
    assert await revalidate_native_output_fences(
        _session_cm, sink, _principal(), destination_kind="remote", multi_workspace_enabled=1,  # type: ignore[arg-type]
    ) is False


def _context() -> dict[str, Any]:
    return {
        "principal": _principal(), "session": object(), "destination_kind": "local",
        "settings": SimpleNamespace(multi_workspace_enabled=FLAG),
    }


@pytest.mark.asyncio
async def test_handlers_pass_principal_scope_and_flag() -> None:
    seen: dict[str, dict[str, Any]] = {}
    source_id, document_id = uuid4(), uuid4()

    def spy(name: str, result: object) -> Any:
        async def call(*_a: object, **kw: object) -> object:
            seen[name] = kw
            return result
        return call

    page = SimpleNamespace(items=[], next_cursor=None)
    tool_source = SimpleNamespace(id=source_id, generation=1, __dict__={"id": source_id})
    with (
        patch("modules.sources.public.list_tool_sources", spy("list_sources", page)),
        patch("modules.sources.public.get_tool_source", spy("get_source", tool_source)),
        patch("modules.knowledge.documents.public.get_tool_document", spy("get_document", None)),
        patch("modules.knowledge.documents.public.list_tool_documents", spy("list_documents", page)),
    ):
        await builtins._handle_sources_list_sources({}, _context())
        await builtins._handle_sources_get_source({"source_id": str(source_id)}, _context())
        await builtins._handle_knowledge_get_document({"document_id": str(document_id)}, _context())
        await builtins._handle_knowledge_list_documents({}, _context())
    assert set(seen) == {"list_sources", "get_source", "get_document", "list_documents"}
    for kwargs in seen.values():
        assert kwargs["scope"] == OWNER and kwargs["multi_workspace_enabled"] is FLAG


def test_missing_flag_denies_handler_scope() -> None:
    context = _context()
    context["settings"] = SimpleNamespace()
    with pytest.raises(PermissionError):
        builtins._principal_scope(context)


@pytest.mark.asyncio
async def test_purge_is_workspace_scoped_and_denies_member_before_query() -> None:
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await purge_browser_results_in_uow(
            session, run_ids=[uuid4()], scope=MEMBER, multi_workspace_enabled=FLAG,
        )
    assert caught.value.status_code == 403
    session.scalars.assert_not_called()

    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    assert await purge_browser_results_in_uow(
        session, run_ids=[uuid4()], scope=OWNER, multi_workspace_enabled=FLAG,
    ) == 0
    text = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "browser_read_jobs.workspace_id =" in text
