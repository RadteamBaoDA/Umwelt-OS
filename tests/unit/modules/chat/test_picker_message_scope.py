"""The ``@`` picker scope is stored on the user message (ids only) and read back for the transcript."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.chat import routes
from modules.chat.models import Message, ResponseRun
from modules.chat.schemas import (
    SOURCE_SCOPE_META_KEY,
    MessageMutationRequest,
    MessageRead,
    SendMessageRequest,
)
from tests.unit.modules.chat.test_workspace_stamp import (
    OWNER,
    _added,
    _conv,
    _patch_routes,
    _session,
)


def test_picker_context_yields_scope_ids_only() -> None:
    """Only a picker-origin context writes the source ids; gadget, selection and plain turns write nothing."""
    from modules.chat._routes.guards import picker_scope_metadata

    a = uuid4()
    picker = {"source_scope": [a], "source_scope_origin": "picker"}
    assert picker_scope_metadata(picker) == {SOURCE_SCOPE_META_KEY: [str(a)]}
    assert picker_scope_metadata({"source_scope": [a]}) == {}
    assert picker_scope_metadata({"source_scope": [a], "source_scope_origin": "gadget"}) == {}
    assert picker_scope_metadata({"source_scope": [], "source_scope_origin": "picker"}) == {}
    assert picker_scope_metadata({}) == {}


def test_message_source_scope_reads_only_string_list() -> None:
    """Malformed stored values read as an empty scope so the transcript shows nothing."""
    from modules.chat._routes.guards import message_source_scope

    assert message_source_scope({SOURCE_SCOPE_META_KEY: ["a", "b"]}) == ["a", "b"]
    assert message_source_scope({SOURCE_SCOPE_META_KEY: "a"}) == []
    assert message_source_scope({SOURCE_SCOPE_META_KEY: [1, None, "c"]}) == ["c"]
    assert message_source_scope(None) == []
    assert message_source_scope({}) == []


def test_message_read_defaults_to_empty_scope() -> None:
    """Messages without a picker scope expose an empty list, not a missing field."""
    read = MessageRead(
        id=uuid4(),
        conversation_id=uuid4(),
        role="user",
        content="hi",
        created_at="2026-11-24T00:00:00Z",
    )
    assert read.source_scope == []


@pytest.mark.asyncio
async def test_send_with_picker_stores_scope_on_user_message(monkeypatch) -> None:
    """A send with sourceIds stores the resolved source ids on the user message in the same transaction."""
    conv = _conv()
    _patch_routes(monkeypatch, conv)
    source = str(uuid4())
    monkeypatch.setattr(
        routes.chat_public,
        "resolve_gadget_context",
        AsyncMock(return_value={"source_scope": [source], "source_scope_origin": "picker"}),
    )
    session = _session()
    await routes.send_message(
        conv.id, SendMessageRequest(content="hi"), MagicMock(), session, OWNER
    )
    (user_msg,) = _added(session, Message)
    assert user_msg.metadata_json == {SOURCE_SCOPE_META_KEY: [source]}


@pytest.mark.asyncio
async def test_send_without_picker_stores_no_scope(monkeypatch) -> None:
    """A plain send leaves the user message metadata untouched."""
    conv = _conv()
    _patch_routes(monkeypatch, conv)
    session = _session()
    await routes.send_message(
        conv.id, SendMessageRequest(content="hi"), MagicMock(), session, OWNER
    )
    (user_msg,) = _added(session, Message)
    assert not user_msg.metadata_json


@pytest.mark.asyncio
async def test_edit_copies_original_prompt_scope(monkeypatch) -> None:
    """An edit's revision user message carries the original prompt's picker scope."""
    conv = _conv()
    _patch_routes(monkeypatch, conv)
    source = str(uuid4())
    target = SimpleNamespace(id=uuid4(), role="user", content="old", response_id=None)
    run = SimpleNamespace(user_message_id=uuid4(), retrieval_context={})
    prompt = SimpleNamespace(content="old", metadata_json={SOURCE_SCOPE_META_KEY: [source]})
    queue = [None, None, target, run, prompt]
    session = _session(AsyncMock(side_effect=lambda *_a, **_k: queue.pop(0)))
    payload = MessageMutationRequest(
        action="edit",
        base_content_hash=hashlib.sha256(b"old").hexdigest(),
        client_request_id="r1",
        content="new",
    )
    await routes.mutate_message(conv.id, target.id, payload, MagicMock(), session, OWNER)
    (revision,) = _added(session, Message)
    assert revision.metadata_json == {SOURCE_SCOPE_META_KEY: [source]}
    assert _added(session, ResponseRun)


def test_message_reads_exposes_scope_of_user_message() -> None:
    """The transcript mapping returns the stored picker ids on the user row and nothing on the answer."""
    from datetime import UTC, datetime

    from modules.chat._routes.mutation import _message_reads

    source = str(uuid4())
    base = {"client_request_id": None, "model_identity": None, "response_id": None}
    user = SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        role="user",
        content="hi",
        revision_of_message_id=None,
        created_at=datetime(2026, 11, 24, tzinfo=UTC),
        metadata_json={SOURCE_SCOPE_META_KEY: [source]},
        **base,
    )
    answer = SimpleNamespace(
        id=uuid4(),
        conversation_id=user.conversation_id,
        role="assistant",
        content="yo",
        revision_of_message_id=None,
        created_at=datetime(2026, 11, 24, tzinfo=UTC),
        metadata_json={},
        **base,
    )
    reads = _message_reads([user, answer], [[], []], {}, {})
    assert reads[0].source_scope == [source]
    assert reads[1].source_scope == []
