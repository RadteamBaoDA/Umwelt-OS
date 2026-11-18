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
    out = await withheld_brief_ids(object(), msgs, scope=object())  # type: ignore[arg-type]
    assert out == {BRIEF, "not-a-uuid"} and len(seen) == 1


@pytest.mark.asyncio
async def test_no_brief_messages_skip_the_lookup() -> None:
    out = await withheld_brief_ids(object(), [{}], scope=object())  # type: ignore[arg-type]
    assert out == frozenset()


def test_brief_messages_never_enter_model_history() -> None:
    """Briefs are standalone assistant posts with no user turn, so ``select_turns`` drops them."""
    brief = _brief_message()
    turns = select_turns([brief], [])
    assert turns == []
