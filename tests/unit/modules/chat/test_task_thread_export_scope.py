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
        multi_workspace_enabled=False,
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


def test_memory_origin_scope_excludes_task_threads() -> None:
    """A Memory origin can never come from a scheduled-task thread (withheld check unavailable there)."""
    origin = _sql(*export._chat_export_scope(NOW, NOW, uuid4(), include_task_threads=False))
    assert "scheduled_task" not in origin and "context_kind != 'automation'" in origin
    assert "scheduled_task" in _sql(*export._chat_export_scope(NOW, NOW, uuid4()))


BRIEF_META = {"origin": "scheduled_task", "brief_id": "b1"}


def _message_fence(withheld: bool | None) -> Any:
    """A message fence carrying the given page-build withheld flag."""
    return SimpleNamespace(
        conversation_id=uuid4(),
        conversation_created_at=NOW,
        conversation_updated_at=NOW,
        message_id=uuid4(),
        message_created_at=NOW,
        message_updated_at=NOW,
        citations=[],
        brief_withheld=withheld,
    )


async def _revalidate_message(
    monkeypatch: pytest.MonkeyPatch, withheld: bool | None, now_withheld: set[str]
) -> Any:
    """Recheck one message fence while the brief lookup reports ``now_withheld``."""
    conv = SimpleNamespace(
        created_at=NOW,
        updated_at=NOW,
        ephemeral=False,
        expires_at=None,
        context_kind="automation",
        metadata_json={"scheduled_task": {}},
    )
    msg = SimpleNamespace(created_at=NOW, updated_at=NOW, metadata_json=BRIEF_META)
    results = iter([conv, msg])
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=lambda _s: MagicMock(one_or_none=MagicMock(return_value=next(results)))
    )
    lookups: list[Any] = []

    async def fake_withheld(_s: Any, metas: Any, **_k: Any) -> frozenset[str]:
        lookups.append(metas)
        return frozenset(now_withheld)

    monkeypatch.setattr(export, "withheld_brief_ids", fake_withheld)
    out = await export._revalidate_export_rows(
        session,
        record_kind="messages",
        fences=[_message_fence(withheld)],
        scope=SimpleNamespace(workspace_id=uuid4()),
        multi_workspace_enabled=False,
    )
    return out, lookups


@pytest.mark.asyncio
async def test_fence_recheck_rejects_brief_withheld_after_page_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, lookups = await _revalidate_message(monkeypatch, False, {"b1"})
    assert out == ("record_changed", []) and lookups == [[BRIEF_META]]
    assert (await _revalidate_message(monkeypatch, False, set()))[0] == (None, [])
    # Already exported as the placeholder, or not a brief: no lookup, nothing to reject.
    out, lookups = await _revalidate_message(monkeypatch, True, {"b1"})
    assert out == (None, []) and lookups == []
    out, lookups = await _revalidate_message(monkeypatch, None, {"b1"})
    assert out == (None, []) and lookups == []


@pytest.mark.asyncio
async def test_export_content_becomes_placeholder_and_fence_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.chat._public.scheduled_task_threads import SCRUBBED_CONTENT

    def row(meta: Any) -> dict[str, Any]:
        return {
            "message_id": uuid4(),
            "conversation_id": uuid4(),
            "role": "assistant",
            "content": "Secret brief prose",
            "citations": [],
            "metadata_json": meta,
            "response_id": None,
            "revision_of_message_id": None,
            "message_created_at": NOW,
            "message_updated_at": NOW,
            "conversation_created_at": NOW,
            "conversation_updated_at": NOW,
        }

    candidates = [(row(BRIEF_META), [], 0), (row(None), [], 0)]

    async def stream(*_a: Any, **_k: Any) -> Any:
        return candidates, set(), False

    async def evidence(*_a: Any, **_k: Any) -> dict[Any, Any]:
        return {}

    async def withheld(*_a: Any, **_k: Any) -> frozenset[str]:
        return frozenset({"b1"})

    monkeypatch.setattr(export, "_stream_export_message_candidates", stream)
    monkeypatch.setattr(export, "_chat_export_evidence_fences", evidence)
    monkeypatch.setattr(export, "withheld_brief_ids", withheld)
    items, fences, _more = await export._export_message_records(
        MagicMock(),
        limit=10,
        row_scope=(),
        snapshot_at=NOW,
        position=None,
        scope=SimpleNamespace(workspace_id=uuid4()),
        multi_workspace_enabled=False,
    )
    assert [i.content for i in items] == [SCRUBBED_CONTENT, "Secret brief prose"]
    assert [f.brief_withheld for f in fences] == [True, None]
