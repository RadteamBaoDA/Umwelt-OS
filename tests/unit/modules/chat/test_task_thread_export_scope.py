"""Scheduled-task threads are searchable and exportable; other automation threads stay excluded."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import and_
from sqlalchemy.dialects import postgresql

from modules.chat._public import export
from modules.chat.public import search_conversations

NOW = datetime.now(UTC)


def _sql(*predicates: Any) -> str:
    """Compile predicates as one AND with bound literals rendered."""
    return str(
        and_(*predicates).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_export_scope_keeps_task_threads_and_excludes_other_automation() -> None:
    sql = _sql(*export._chat_export_scope_retained(NOW))
    assert "context_kind != 'automation'" in sql
    assert "scheduled_task" in sql  # has_key predicate lets task threads through


@pytest.mark.asyncio
async def test_search_uses_the_same_task_thread_predicate() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    scope = SimpleNamespace(workspace_id=uuid4(), user_id=7)
    with patch(
        "modules.chat._public.export._chat_export_privacy",
        AsyncMock(return_value=(True, True, None)),
    ):
        await search_conversations(
            session,
            "Daily brief",
            scope=scope,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
            limit=5,
            offset=0,
            archived=False,
        )
    compiled = session.scalars.await_args.args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    assert "scheduled_task" in str(compiled)


async def _revalidate(kind: str | None, metadata: dict[str, Any] | None) -> Any:
    """Run the fence recheck for one conversation fence against a stubbed row."""
    row = SimpleNamespace(
        created_at=NOW,
        updated_at=NOW,
        ephemeral=False,
        expires_at=None,
        context_kind=kind,
        metadata_json=metadata,
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))
    fence = SimpleNamespace(
        conversation_id=uuid4(),
        conversation_created_at=NOW,
        conversation_updated_at=NOW,
        message_id=None,
        citations=[],
    )
    return await export._revalidate_export_rows(
        session,
        record_kind="conversations",
        fences=[fence],  # type: ignore[list-item]
        scope=SimpleNamespace(workspace_id=uuid4()),
    )


@pytest.mark.asyncio
async def test_fence_recheck_accepts_task_thread_and_rejects_other_automation() -> None:
    assert await _revalidate("automation", {"scheduled_task": {"kind": "daily_brief"}}) == (
        None,
        [],
    )
    assert await _revalidate("automation", {}) == ("record_changed", [])
    assert await _revalidate("automation", None) == ("record_changed", [])
    assert await _revalidate(None, None) == (None, [])
