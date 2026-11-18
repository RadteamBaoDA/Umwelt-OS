"""Scheduled-task thread commands: idempotent conversation/message writes and brief scrubbing."""

from typing import Any
from uuid import uuid4

import pytest

import core.workspaces.models  # noqa: F401  (registers the workspaces table for FK resolution)
from core.workspaces.schemas import WorkspaceContext
from modules.chat._public import scheduled_task_threads as threads
from modules.chat.models import Conversation, Message

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)


class FakeSession:
    """In-memory AsyncSession stand-in that routes selects by the table they read."""

    def __init__(self) -> None:
        self.conversations: list[Conversation] = []
        self.messages: list[Message] = []

    def add(self, row: Any) -> None:
        """Assign the id a flush would and store the row."""
        row.id = row.id or uuid4()
        if isinstance(row, Conversation):
            row.metadata_json = row.metadata_json or {}
            self.conversations.append(row)
        else:
            row.citations = row.citations or []
            self.messages.append(row)

    async def flush(self) -> None:
        return None

    async def scalar(self, stmt: Any) -> Any:
        sql = str(stmt)
        if "FROM chat_conversations" in sql:
            rows = self.conversations
            if "chat_messages" not in sql and "SELECT chat_conversations.id" in sql:
                return rows[0].id if rows else None
            return rows[0] if rows else None
        return next((m.id for m in self.messages if m.client_request_id == self._rid(stmt)), None)

    @staticmethod
    def _rid(stmt: Any) -> Any:
        return next(v for k, v in stmt.compile().params.items() if str(v).startswith("task:"))

    async def scalars(self, _stmt: Any) -> list[Message]:
        return [m for m in self.messages if "brief_id" in m.metadata_json]


async def _conv(session: FakeSession, kind: Any = "daily_brief") -> Any:
    return await threads.ensure_automation_conversation(
        session,
        uuid4(),
        "Daily brief",
        task_kind=kind,
        scope=SCOPE,  # type: ignore[arg-type]
    )


async def _append(session: FakeSession, cid: Any, key: str, ids: list[Any], **kw: Any) -> Any:
    return await threads.append_task_message(
        session,  # type: ignore[arg-type]
        cid,
        content="x",
        citations=[],
        metadata=kw.get("metadata", {}),
        source_ids=ids,
        dedupe_key=key,
        scope=SCOPE,
    )


@pytest.mark.asyncio
async def test_ensure_conversation_creates_once_and_marks_task() -> None:
    s = FakeSession()
    aid = uuid4()
    a = await threads.ensure_automation_conversation(
        s,
        aid,
        "Brief",
        task_kind="daily_brief",
        scope=SCOPE,  # type: ignore[arg-type]
    )
    b = await threads.ensure_automation_conversation(
        s,
        aid,
        "Brief",
        task_kind="daily_brief",
        scope=SCOPE,  # type: ignore[arg-type]
    )
    assert a == b and len(s.conversations) == 1
    assert s.conversations[0].metadata_json == {"scheduled_task": {"kind": "daily_brief"}}
    assert s.conversations[0].context_kind == "automation"


@pytest.mark.asyncio
async def test_ensure_conversation_without_kind_keeps_metadata_empty() -> None:
    s = FakeSession()
    await threads.ensure_automation_conversation(
        s,
        uuid4(),
        "Rule",
        task_kind=None,
        scope=SCOPE,  # type: ignore[arg-type]
    )
    assert s.conversations[0].metadata_json == {}
    assert s.conversations[0].title == "Automation: Rule"


@pytest.mark.asyncio
async def test_append_is_idempotent_on_dedupe_key() -> None:
    s = FakeSession()
    cid = await _conv(s)
    first = await _append(s, cid, "brief:X", [])
    second = await _append(s, cid, "brief:X", [])
    assert first[1] is True and second == (first[0], False)
    assert len(s.messages) == 1
    assert s.messages[0].client_request_id == "task:brief:X"
    assert s.messages[0].metadata_json["origin"] == "scheduled_task"


@pytest.mark.asyncio
async def test_append_merges_source_scope_newest_first_capped() -> None:
    s = FakeSession()
    cid = await _conv(s)
    ids = [uuid4() for _ in range(101)]
    for i, sid in enumerate(ids):
        await _append(s, cid, f"k{i}", [sid, ids[0]])
    scope = s.conversations[0].metadata_json["task_source_scope"]
    assert len(scope) == 100 and len(set(scope)) == 100
    assert scope[0] == str(ids[100])


@pytest.mark.asyncio
async def test_scrub_task_messages_clears_content_and_citations() -> None:
    s = FakeSession()
    cid = await _conv(s)
    await _append(s, cid, "brief:B", [], metadata={"brief_id": "B", "brief_citations": [1]})
    s.messages[0].citations = [{"a": 1}]
    assert await threads.scrub_task_messages(s, ["B"], scope=SCOPE) == 1  # type: ignore[arg-type]
    m = s.messages[0]
    assert m.content == threads.SCRUBBED_CONTENT
    assert m.citations == [] and "brief_citations" not in m.metadata_json
