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

    async def live(_s: Any, ids: list[UUID], **_k: Any) -> set[UUID]:
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
async def test_get_conversation_withholds_on_its_own_session_before_the_citation_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route wiring: the lookup uses a fresh factory session and runs before citation locks."""
    from modules.chat import routes
    from tests.unit.modules.chat._routes_patch import patch_routes
    from tests.unit.modules.chat.test_p15_w3_api import _ConversationSession

    msg = _brief_message()
    main = _ConversationSession([msg], [])
    own = object()
    order: list[str] = []

    class _Factory:
        def __call__(self) -> "_Factory":
            return self

        async def __aenter__(self) -> object:
            return own

        async def __aexit__(self, *_a: Any) -> None:
            order.append("released")

    async def lookup(session: Any, metas: Any, **kwargs: Any) -> frozenset[str]:
        order.append("lookup")
        assert session is own and kwargs["multi_workspace_enabled"] is False
        assert metas == [msg.metadata_json]
        return frozenset({BRIEF})

    async def keep(_s: Any, lists: Any, _scope: Any = None) -> list[list[Any]]:
        order.append("citations")
        return [list(x) for x in lists]

    async def default_scope(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(workspace_id=uuid4(), user_id=1)

    monkeypatch.setattr(routes, "withheld_brief_ids", lookup)
    monkeypatch.setattr(routes, "multi_workspace_enabled", lambda: False)
    patch_routes(monkeypatch, "_filter_citation_lists", keep)
    patch_routes(monkeypatch, "owner_default_scope", default_scope)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=_Factory()))
    )
    detail = await routes.get_conversation(
        msg.conversation_id,
        main,  # type: ignore[arg-type]
        SimpleNamespace(owner_id=1),  # type: ignore[arg-type]
        SimpleNamespace(headers={}),  # type: ignore[arg-type]
        request,  # type: ignore[arg-type]
    )
    assert order == ["lookup", "released", "citations"]
    assert detail.messages[0].content == SCRUBBED_CONTENT and detail.messages[0].task is None
