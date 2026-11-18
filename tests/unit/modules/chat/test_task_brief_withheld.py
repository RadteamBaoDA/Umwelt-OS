"""A scheduled-task brief whose sources stopped being eligible is withheld from reads and history."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from modules.chat._public.scheduled_task_threads import SCRUBBED_CONTENT, withheld_brief_ids
from modules.chat._routes.mutation import _message_reads
from modules.chat.history import select_turns

BRIEF = str(uuid4())


def _brief_message(brief_id: str = BRIEF) -> Any:
    """A stored assistant brief message as the scheduler posts it."""
    return SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        role="assistant",
        content="Secret brief prose",
        client_request_id=None,
        model_identity=None,
        response_id=None,
        revision_of_message_id=None,
        created_at=datetime.now(UTC),
        citations=[],
        metadata_json={
            "origin": "scheduled_task",
            "task_kind": "daily_brief",
            "run_id": "r1",
            "brief_id": brief_id,
            "brief_revision": 1,
            "brief_citations": [],
        },
    )


def test_withheld_brief_is_scrubbed_in_reads() -> None:
    m = _brief_message()
    read = _message_reads([m], [[{"x": 1}]], {}, {}, frozenset({BRIEF}))[0]
    assert read.content == SCRUBBED_CONTENT and read.task is None and read.citations == []


def test_live_brief_is_unaffected_in_reads() -> None:
    m = _brief_message()
    read = _message_reads([m], [[]], {}, {}, frozenset())[0]
    assert read.content == "Secret brief prose" and read.task is not None


@pytest.mark.asyncio
async def test_ineligible_brief_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.dashboard import public as dashboard

    live_id = str(uuid4())
    seen: list[list[UUID]] = []

    async def live(_s: Any, ids: list[UUID], **k: Any) -> set[UUID]:
        assert k["lock"] is False  # withholding never takes row locks
        seen.append(ids)
        return {UUID(live_id)}

    monkeypatch.setattr(dashboard, "live_brief_ids", live)
    plain = SimpleNamespace(metadata_json=None)
    msgs = [
        _brief_message(BRIEF).metadata_json,
        _brief_message(live_id).metadata_json,
        _brief_message("not-a-uuid").metadata_json,
        plain.metadata_json,
    ]
    out = await withheld_brief_ids(  # type: ignore[arg-type]
        object(), msgs, scope=object(), multi_workspace_enabled=False
    )
    assert out == {BRIEF, "not-a-uuid"} and len(seen) == 1


@pytest.mark.asyncio
async def test_no_brief_messages_skip_the_lookup() -> None:
    out = await withheld_brief_ids(  # type: ignore[arg-type]
        object(), [{}], scope=object(), multi_workspace_enabled=False
    )
    assert out == frozenset()


def test_brief_messages_never_enter_model_history() -> None:
    """Briefs are standalone assistant posts with no user turn, so ``select_turns`` drops them."""
    brief = _brief_message()
    turns = select_turns([brief], [])
    assert turns == []


@pytest.mark.asyncio
async def test_malformed_task_metadata_is_withheld_not_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """origin=scheduled_task with an unknown key and a bad brief_id fails closed end to end."""
    from modules.dashboard import public as dashboard

    async def live(_s: Any, ids: list[UUID], **_k: Any) -> set[UUID]:
        return set(ids)

    monkeypatch.setattr(dashboard, "live_brief_ids", live)
    bad = {"origin": "scheduled_task", "surprise": 1, "brief_id": "zzz"}
    missing = {"origin": "scheduled_task"}
    withheld = await withheld_brief_ids(  # type: ignore[arg-type]
        object(), [bad, missing], scope=object(), multi_workspace_enabled=False
    )
    assert withheld == {"zzz", ""}
    m = _brief_message("zzz")
    m.metadata_json = bad
    read = _message_reads([m], [[{"x": 1}]], {}, {}, withheld)[0]
    assert read.content == SCRUBBED_CONTENT and read.task is None and read.citations == []


def test_task_brief_key() -> None:
    from modules.chat._public.scheduled_task_threads import task_brief_key

    assert task_brief_key(None) is None and task_brief_key({"origin": "x"}) is None
    assert task_brief_key({"origin": "scheduled_task"}) == ""
    assert task_brief_key({"origin": "scheduled_task", "brief_id": 5}) == "5"


@pytest.mark.asyncio
async def test_get_conversation_withholds_on_the_request_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route wiring: the lock-free lookup runs on the request session and scrubs the brief."""
    from modules.chat import routes
    from tests.unit.modules.chat._routes_patch import patch_routes
    from tests.unit.modules.chat.test_p15_w3_api import _ConversationSession

    msg = _brief_message()
    main = _ConversationSession([msg], [])

    async def lookup(session: Any, metas: Any, **kwargs: Any) -> frozenset[str]:
        assert session is main and kwargs["multi_workspace_enabled"] is False
        assert metas == [msg.metadata_json]
        return frozenset({BRIEF})

    async def keep(_s: Any, lists: Any, _scope: Any = None) -> list[list[Any]]:
        return [list(x) for x in lists]

    async def default_scope(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(workspace_id=uuid4(), user_id=1)

    monkeypatch.setattr(routes, "withheld_brief_ids", lookup)
    monkeypatch.setattr(routes, "multi_workspace_enabled", lambda: False)
    patch_routes(monkeypatch, "_filter_citation_lists", keep)
    patch_routes(monkeypatch, "owner_default_scope", default_scope)
    detail = await routes.get_conversation(
        msg.conversation_id,
        main,  # type: ignore[arg-type]
        SimpleNamespace(owner_id=1),  # type: ignore[arg-type]
        SimpleNamespace(headers={}),  # type: ignore[arg-type]
    )
    assert detail.messages[0].content == SCRUBBED_CONTENT and detail.messages[0].task is None


# --- topic watch posts: withheld by their Source, never by brief liveness -----------------------

TOPIC_SOURCE = str(uuid4())


def _topic_meta(source_id: str = TOPIC_SOURCE) -> dict[str, Any]:
    """Stored metadata of a topic-watch post."""
    return {
        "origin": "scheduled_task",
        "task_kind": "topic_watch",
        "run_id": "r1",
        "source_id": source_id,
        "items": [{"title": "T", "url": "https://example.com/a", "site": "example.com"}],
        "stats": {"items": 1},
        "coverage": "complete",
    }


def test_brief_key_ignores_topic_posts() -> None:
    from modules.chat._public.scheduled_task_threads import task_brief_key, topic_source_key

    assert task_brief_key(_topic_meta()) is None
    assert topic_source_key(_topic_meta()) == TOPIC_SOURCE
    assert topic_source_key({"origin": "scheduled_task", "task_kind": "topic_watch"}) == ""
    assert topic_source_key(_brief_message().metadata_json) is None
    assert topic_source_key({"task_kind": "topic_watch"}) is None  # origin gate


@pytest.mark.asyncio
async def test_topic_posts_withheld_for_inactive_local_missing_or_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.chat._public import scheduled_task_threads as threads

    live, archived, local, missing = (str(uuid4()) for _ in range(4))
    calls: list[list[UUID]] = []

    async def fences(_s: Any, ids: list[UUID], **_k: Any) -> dict[UUID, Any]:
        calls.append(ids)
        table = {
            live: ("active", False),
            archived: ("archived", False),
            local: ("active", True),
        }
        return {
            UUID(k): SimpleNamespace(status=v[0], local_only=v[1])
            for k, v in table.items()
            if UUID(k) in ids
        }

    monkeypatch.setattr(threads.sources_public, "get_source_fences", fences)
    metas = [_topic_meta(s) for s in (live, archived, local, missing, "not-a-uuid", "")]
    metas += [_brief_message().metadata_json, None]
    out = await threads.withheld_topic_messages(  # type: ignore[arg-type]
        object(), metas, scope=object(), multi_workspace_enabled=False
    )
    assert out == {archived, local, missing, "not-a-uuid", ""} and len(calls) == 1


@pytest.mark.asyncio
async def test_no_topic_posts_skip_the_source_lookup() -> None:
    from modules.chat._public.scheduled_task_threads import withheld_topic_messages

    out = await withheld_topic_messages(  # type: ignore[arg-type]
        object(), [_brief_message().metadata_json], scope=object(), multi_workspace_enabled=False
    )
    assert out == frozenset()


def test_withheld_topic_post_is_scrubbed_in_reads() -> None:
    m = _brief_message()
    m.metadata_json = _topic_meta()
    m.content = "- Secret: summary"
    read = _message_reads([m], [[{"x": 1}]], {}, {}, frozenset(), frozenset({TOPIC_SOURCE}))[0]
    assert read.content == SCRUBBED_CONTENT and read.task is None and read.citations == []
    live = _message_reads([m], [[]], {}, {}, frozenset(), frozenset())[0]
    assert live.content == "- Secret: summary" and live.task is not None


def test_read_and_export_share_one_withhold_rule() -> None:
    """Export and read paths call the same pure predicate, so a topic post scrubs on both."""
    from modules.chat._public.scheduled_task_threads import is_withheld_task_message as gone

    assert gone(_topic_meta(), frozenset(), frozenset({TOPIC_SOURCE}))
    assert not gone(
        _topic_meta(), frozenset({TOPIC_SOURCE}), frozenset()
    )  # brief set is irrelevant
    assert gone(_brief_message().metadata_json, frozenset({BRIEF}), frozenset())
    assert not gone(_brief_message().metadata_json, frozenset(), frozenset({BRIEF}))
    assert not gone({"role": "x"}, frozenset({BRIEF}), frozenset({TOPIC_SOURCE}))
    assert not gone(None, frozenset(), frozenset())


def test_export_module_wires_topic_withholding() -> None:
    """The export page and its final-fence recheck both use the topic lookup (source-level check)."""
    import inspect

    from modules.chat._public import export

    src = inspect.getsource(export)
    assert src.count("withheld_topic_messages(") >= 2 and "is_withheld_task_message(" in src
