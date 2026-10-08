"""Workspace-scope contracts of Memory: admission before query, scoped SQL, facade call forms."""

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.memory import public
from modules.memory.public import MemoryService

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=2)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=3)
FENCE = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=1)
CTX = {"scope": OWNER, "multi_workspace_enabled": False}


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_service_methods_require_scope_and_flag_keywords() -> None:
    """C-2: the KnowledgeService facade calls these forms exactly."""
    for name in ("get_memories", "get_active_memory_context"):
        params = inspect.signature(getattr(MemoryService, name)).parameters
        for key in ("scope", "multi_workspace_enabled"):
            assert params[key].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[key].default is inspect.Parameter.empty


def test_every_content_method_requires_scope() -> None:
    for name, member in inspect.getmembers(MemoryService, inspect.iscoroutinefunction):
        if name.startswith("_"):
            continue
        params = inspect.signature(member).parameters
        assert params["scope"].default is inspect.Parameter.empty, name
        assert params["multi_workspace_enabled"].default is inspect.Parameter.empty, name


@pytest.mark.parametrize("call", [
    lambda svc: svc.get_memories(**{**CTX, "scope": MEMBER}),
    lambda svc: svc.get_active_memory_context(**{**CTX, "scope": MEMBER}),
    lambda svc: svc.get_candidates(**{**CTX, "scope": MEMBER}),
    lambda svc: svc.get_privacy_config(**{**CTX, "scope": MEMBER}),
    lambda svc: svc.forget_memory(uuid4(), **{**CTX, "scope": MEMBER}),
])
async def test_member_is_denied_before_any_query(call) -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    session.scalars = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await call(MemoryService(session))
    assert caught.value.status_code == 403
    session.execute.assert_not_awaited()
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()


async def test_missing_flag_is_rejected() -> None:
    with pytest.raises(TypeError):
        await public._admit(MagicMock(), scope=OWNER, multi_workspace_enabled=None)  # type: ignore[arg-type]


async def test_get_memories_filters_workspace_before_limit() -> None:
    session = MagicMock()
    seen: list[str] = []

    async def scalars(statement: object) -> MagicMock:
        seen.append(_sql(statement))
        return MagicMock(all=list)

    session.scalars = scalars
    with patch.object(public.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)) as admit, \
            patch.object(public, "lock_export_privacy", AsyncMock()):
        await MemoryService(session).get_memories(limit=5, **CTX)
    admit.assert_awaited_once()
    sql = seen[0]
    assert "memories.workspace_id" in sql
    assert sql.index("memories.workspace_id") < sql.index("LIMIT")


def test_export_scope_is_workspace_bound() -> None:
    now = datetime.now(UTC)
    for kind, table in (("memories", "memories"), ("candidates", "memory_candidates")):
        clauses = public._memory_export_scope(kind, now, OWNER)
        assert any(f"{table}.workspace_id" in _sql(clause) for clause in clauses)


async def test_export_page_rejects_foreign_owner_id_before_query() -> None:
    session = MagicMock()
    session.scalar = AsyncMock()
    with patch.object(public.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch.object(public, "lock_export_privacy", AsyncMock()), \
            pytest.raises(ValueError):
        await public.export_page(session, owner_id=99, record_kind="memories", **CTX)
    session.scalar.assert_not_awaited()


async def test_read_export_privacy_is_scoped_and_admitted() -> None:
    session = MagicMock()
    captured: list[str] = []

    async def execute(statement: object) -> MagicMock:
        captured.append(_sql(statement))
        return MagicMock(one_or_none=lambda: None)

    session.execute = execute
    with patch.object(public.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)) as admit:
        result = await public.read_export_privacy(session, **CTX)
    admit.assert_awaited_once()
    assert result.persisted is False
    assert "memory_privacy_settings.workspace_id" in captured[0]
    with pytest.raises(HTTPException):
        await public.read_export_privacy(session, **{**CTX, "scope": MEMBER})


async def test_cache_eviction_is_per_workspace() -> None:
    redis = MagicMock()
    redis.delete = AsyncMock()
    job = InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=2)
    await public.invalidate_memory_cache(redis, scope=job)
    redis.delete.assert_awaited_once_with(f"{public.CACHE_KEY_MEMORIES_ACTIVE}:{WS}")
    assert public._actor(job) == 7 and public._actor(OWNER) == 7
