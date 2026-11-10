"""Post-answer title/suggestions step, tool.status payloads and the rename flag (chat UX package 5)."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.chat._worker import followup, web
from modules.chat._worker.followup import (
    _followup_messages,
    _parse_followup,
    _title_is_auto,
    generate_followup,
)
from modules.chat.schemas import (
    SUGGESTIONS_KEY,
    TITLE_SOURCE_KEY,
    ConversationPatch,
    MessageRead,
)


def _conv(title: str = "New conversation", meta: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(title=title, metadata_json=meta or {})


def _env(ephemeral: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        claimed=SimpleNamespace(ephemeral=ephemeral),
        c=SimpleNamespace(response_id=uuid4(), conversation_id=uuid4(), seq=5),
        turn=SimpleNamespace(user_prompt="hello"),
        setup=SimpleNamespace(),
    )


def test_title_is_auto_rules() -> None:
    assert _title_is_auto(_conv(), "anything")
    assert _title_is_auto(_conv("How do I plan a trip"), "How do I plan a trip to Hue in May?")
    assert not _title_is_auto(_conv("My custom name"), "How do I plan a trip")
    assert not _title_is_auto(_conv(meta={TITLE_SOURCE_KEY: "user"}), "x")
    assert not _title_is_auto(_conv(meta={TITLE_SOURCE_KEY: "auto"}), "x")


def test_parse_followup_bounds_and_cleans() -> None:
    raw = (
        '```json\n{"title": "  \\"A very long title '
        + "x" * 80
        + '.\\" ", "suggestions": ["Q1?", "Q1?", " Q2? ", "Q3?", "Q4?"]}\n```'
    )
    title, suggestions = _parse_followup(raw, want_title=True)
    assert title is not None and len(title) <= 60 and not title.endswith('"')
    assert suggestions == ["Q1?", "Q2?", "Q3?"]


def test_parse_followup_title_ignored_when_not_wanted_and_few_suggestions_dropped() -> None:
    assert _parse_followup('{"title": "T", "suggestions": ["only one"]}', want_title=False) == (
        None,
        [],
    )


@pytest.mark.parametrize("raw", ["", "no json", "{bad", "[1,2]", '{"suggestions": "x"}'])
def test_parse_followup_malformed_is_empty(raw: str) -> None:
    assert _parse_followup(raw, want_title=True) == (None, [])


def test_prompt_asks_for_title_only_when_wanted() -> None:
    assert '"title"' in _followup_messages("u", "a", True)[0]["content"]
    assert '"title"' not in _followup_messages("u", "a", False)[0]["content"]


@pytest.mark.asyncio
async def test_generate_followup_persists_title_and_suggestions() -> None:
    env, mid = _env(), uuid4()
    persist = AsyncMock()
    with (
        patch.object(followup, "_read_turn_texts", AsyncMock(return_value=(True, "hi"))),
        patch.object(followup, "_ask_model", AsyncMock(return_value=("Trip", ["a?", "b?"]))),
        patch.object(followup, "_persist_followup", persist),
    ):
        await generate_followup(env, mid, "answer")  # type: ignore[arg-type]
    persist.assert_awaited_once_with(env, mid, "answer", "Trip", ["a?", "b?"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("boom"), ValueError("x")])
async def test_generate_followup_swallows_every_failure(error: Exception) -> None:
    persist = AsyncMock()
    with (
        patch.object(followup, "_read_turn_texts", AsyncMock(return_value=(True, "hi"))),
        patch.object(followup, "_ask_model", AsyncMock(side_effect=error)),
        patch.object(followup, "_persist_followup", persist),
    ):
        await generate_followup(_env(), uuid4(), "answer")  # type: ignore[arg-type]
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_followup_does_not_swallow_cancellation() -> None:
    with (
        patch.object(followup, "_read_turn_texts", AsyncMock(return_value=(True, "hi"))),
        patch.object(followup, "_ask_model", AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await generate_followup(_env(), uuid4(), "answer")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_generate_followup_skips_ephemeral_and_empty_answers() -> None:
    ask = AsyncMock()
    with (
        patch.object(followup, "_ask_model", ask),
        patch.object(followup, "_read_turn_texts", AsyncMock(return_value=(True, ""))),
    ):
        await generate_followup(_env(ephemeral=True), uuid4(), "answer")  # type: ignore[arg-type]
        await generate_followup(_env(), uuid4(), "   ")  # type: ignore[arg-type]
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_model_uses_answer_provider_and_config_only_fence() -> None:
    env = _env()
    fence = SimpleNamespace(config_only_before_send=AsyncMock())
    env.setup = SimpleNamespace(
        ai_config="cfg", send_fence=fence, role="chat", mapping="m", policy="p"
    )
    gateway = MagicMock()
    reply = json.dumps({"title": "T", "suggestions": ["a?", "b?"]})
    gateway.chat = AsyncMock(return_value={"choices": [{"message": {"content": reply}}]})
    with patch.object(followup, "_build_gateway", return_value=gateway) as build:
        result = await followup._ask_model(env, "answer", True)  # type: ignore[arg-type]
    assert result == ("T", ["a?", "b?"])
    build.assert_called_once_with(env.c, "cfg", fence, config_only=True)
    args = gateway.chat.await_args
    assert args.args[:3] == ("chat", "m", "p")
    assert args.kwargs["before_send"] is fence.config_only_before_send


@pytest.mark.asyncio
async def test_persist_followup_writes_title_suggestions_and_events() -> None:
    env = _env()
    conversation = _conv("hello there")
    message = SimpleNamespace(content="answer", metadata_json={"reasoning": "r"})
    run = SimpleNamespace(status="completed")
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[run, conversation, "hello there friend"])
    session.get = AsyncMock(return_value=message)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    env.c.session_factory = lambda: session
    events: list[tuple[str, dict]] = []
    with (
        patch.object(followup, "_next_event_seq", AsyncMock(side_effect=[6, 7])),
        patch.object(
            followup, "_add_event", lambda s, c, seq, kind, data: events.append((kind, data))
        ),
    ):
        mid = uuid4()
        await followup._persist_followup(env, mid, "answer", "Hello", ["a?", "b?"])  # type: ignore[arg-type]
    assert conversation.title == "Hello"
    assert conversation.metadata_json[TITLE_SOURCE_KEY] == "auto"
    assert message.metadata_json[SUGGESTIONS_KEY] == ["a?", "b?"]
    assert message.metadata_json["reasoning"] == "r"
    assert [k for k, _ in events] == ["conversation.title", "message.suggestions"]
    assert events[1][1] == {"message_id": str(mid), "suggestions": ["a?", "b?"]}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_followup_never_overwrites_renamed_title_or_redacted_answer() -> None:
    env = _env()
    renamed = _conv("Mine", meta={TITLE_SOURCE_KEY: "user"})
    message = SimpleNamespace(content="answer", metadata_json={})
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[SimpleNamespace(status="completed"), renamed, "x"])
    session.get = AsyncMock(return_value=message)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    env.c.session_factory = lambda: session
    with (
        patch.object(followup, "_next_event_seq", AsyncMock(return_value=6)),
        patch.object(followup, "_add_event", lambda *a: None),
    ):
        await followup._persist_followup(env, uuid4(), "answer", "Auto", [])  # type: ignore[arg-type]
        assert renamed.title == "Mine"
        # Message content changed (redaction/edit): nothing is written at all.
        session.scalar = AsyncMock(side_effect=[SimpleNamespace(status="completed"), _conv(), "x"])
        message.content = "[redacted]"
        await followup._persist_followup(env, uuid4(), "answer", "Auto", ["a?", "b?"])  # type: ignore[arg-type]
    assert SUGGESTIONS_KEY not in message.metadata_json


@pytest.mark.asyncio
async def test_tool_status_payload_is_additive() -> None:
    added: list[object] = []
    session = MagicMock()
    session.add = added.append
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(web, "_lock_live_response", AsyncMock()),
        patch.object(web, "_next_event_seq", AsyncMock(return_value=9)),
    ):
        for kwargs in ({}, {"label": "table-skill"}, {"detail": {"documents": 3}}):
            await web._publish_tool_status(
                lambda: session,
                uuid4(),
                uuid4(),
                None,
                8,
                "retrieval",
                "done",
                scope=MagicMock(),
                access_fence=MagicMock(),
                **kwargs,
            )  # type: ignore[arg-type]
    payloads = [e.data for e in added]  # type: ignore[attr-defined]
    assert payloads[0] == {"tool": "retrieval", "state": "done"}  # old shape unchanged
    assert payloads[1] == {"tool": "retrieval", "state": "done", "label": "table-skill"}
    assert payloads[2] == {"tool": "retrieval", "state": "done", "detail": {"documents": 3}}


@pytest.mark.asyncio
async def test_data_tool_is_wrapped_in_running_done_and_done_on_error() -> None:
    from modules.chat._worker import stream

    calls: list[tuple] = []

    async def fake_status(c, tool, state, **kw):
        calls.append((tool, state, kw.get("label")))

    handler = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("tool failed")))
    with (
        patch.object(stream, "_publish_status", fake_status),
        patch.dict(stream._TOOL_HANDLERS, {"read_skill": handler}),
        pytest.raises(RuntimeError),
    ):
        await stream._run_data_tool(
            SimpleNamespace(c=object()),  # type: ignore[arg-type]
            {"name": "read_skill", "arguments": '{"name": "tables"}'},
        )
    assert calls == [("read_skill", "running", "tables"), ("read_skill", "done", "tables")]


@pytest.mark.asyncio
async def test_self_reporting_tool_gets_no_generic_status() -> None:
    from modules.chat._worker import stream

    publish = AsyncMock()
    handler = SimpleNamespace(run=AsyncMock(return_value="ok"))
    with (
        patch.object(stream, "_publish_status", publish),
        patch.dict(stream._TOOL_HANDLERS, {"query_collected_data": handler}),
    ):
        out = await stream._run_data_tool(
            SimpleNamespace(c=object()),  # type: ignore[arg-type]
            {"name": "query_collected_data", "arguments": "{}"},
        )
    assert out == "ok"
    publish.assert_not_awaited()


def test_message_read_exposes_suggestions_from_metadata() -> None:
    from modules.chat._routes.guards import _message_suggestions

    assert _message_suggestions({SUGGESTIONS_KEY: ["a?", "", 3, "b?"]}) == ["a?", "b?"]
    assert _message_suggestions(None) == []
    assert _message_suggestions({SUGGESTIONS_KEY: "nope"}) == []
    read = MessageRead(
        id=uuid4(),
        conversation_id=uuid4(),
        role="assistant",
        content="x",
        created_at=datetime.now(UTC),
    )
    assert read.suggestions == []


@pytest.mark.asyncio
async def test_patch_conversation_marks_title_as_user_renamed() -> None:
    from modules.chat import routes

    conv = SimpleNamespace(
        id=uuid4(),
        title="excerpt",
        context_kind=None,
        context_resource_id=None,
        pinned=False,
        archived=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        metadata_json={"date": "2026-01-01"},
        ephemeral=False,
        expires_at=None,
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=conv)
    session.commit = AsyncMock()

    async def refresh(obj: SimpleNamespace) -> None:
        obj.updated_at = datetime.now(UTC)

    session.refresh = refresh
    owner = SimpleNamespace(owner_id=1)
    scope = SimpleNamespace(workspace_id=uuid4())
    with patch.object(routes, "owner_default_scope", AsyncMock(return_value=scope)):
        await routes.patch_conversation(conv.id, ConversationPatch(pinned=True), session, owner)  # type: ignore[arg-type]
        assert TITLE_SOURCE_KEY not in conv.metadata_json  # pin alone is not a rename
        await routes.patch_conversation(conv.id, ConversationPatch(title="excerpt"), session, owner)  # type: ignore[arg-type]
        assert TITLE_SOURCE_KEY not in conv.metadata_json  # unchanged title is not a rename
        await routes.patch_conversation(conv.id, ConversationPatch(title="Renamed"), session, owner)  # type: ignore[arg-type]
    assert conv.title == "Renamed"
    assert conv.metadata_json == {"date": "2026-01-01", TITLE_SOURCE_KEY: "user"}
