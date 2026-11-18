"""Chat tool ``draft_scheduled_task``: attaches a card to the answer and never writes automations."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import modules.automations.public as automations_public
from modules.chat._worker import tools as worker_tools
from modules.chat._worker.finalize import _assistant_metadata
from modules.chat._worker.setup import _task_draft_offered
from modules.chat.generative_ui.tool_specs import build_chat_tool_specs
from modules.chat.task_drafts import (
    DRAFT_SECTIONS,
    TOOL_DRAFT_SCHEDULED_TASK,
    ScheduledTaskDraft,
    parse_task_draft,
)
from modules.dashboard import public as dashboard_public

VALID: dict[str, Any] = {
    "kind": "daily_brief",
    "name": "Morning brief",
    "time": "07:00",
    "days": "weekdays",
    "sections": ["stories", "weather"],
    "extra_instruction": "Reply in Vietnamese",
}


def _env(on: bool = True) -> Any:
    return SimpleNamespace(
        setup=SimpleNamespace(task_draft_on=on), ts=SimpleNamespace(task_draft=None)
    )


def _run(env: Any, args: dict[str, Any]) -> dict[str, Any]:
    handler = worker_tools._TOOL_HANDLERS[TOOL_DRAFT_SCHEDULED_TASK]
    out = asyncio.run(handler.run(env, {"arguments": json.dumps(args)}))
    return json.loads(out)  # type: ignore[no-any-return]


def test_draft_never_saves_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("automations write from chat")

    for name in ("create_automation", "update_automation"):
        if hasattr(automations_public, name):
            monkeypatch.setattr(automations_public, name, boom)
    env = _env()
    result = _run(env, VALID)
    assert result["saved"] is False
    assert result["status"] == "shown_to_owner"
    assert env.ts.task_draft["time"] == "07:00"
    assert env.ts.task_draft["sections"] == ["stories", "weather"]
    # The handler touches no DB session at all (env carries none).
    assert not hasattr(env, "c")


@pytest.mark.parametrize(
    "patch",
    [
        {"time": "25:00"},
        {"sections": ["stories", "stories"]},
        {"kind": "topic_watch"},
        {"surprise": 1},
    ],
)
def test_invalid_draft_is_reported_not_stored(patch: dict[str, Any]) -> None:
    env = _env()
    result = _run(env, {**VALID, **patch})
    assert result["status"] == "invalid"
    assert env.ts.task_draft is None


def test_tool_offered_only_in_plain_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("modules.chat._worker.setup.is_gadget_turn", lambda _p: False)

    def turn(scoped: bool, selected: bool) -> Any:
        return SimpleNamespace(
            answer_request=SimpleNamespace(source_scope_enforced=scoped, selected_only=selected),
            req_params={},
        )

    assert _task_draft_offered(turn(False, False)) is True
    assert _task_draft_offered(turn(True, False)) is False
    assert _task_draft_offered(turn(False, True)) is False
    monkeypatch.setattr("modules.chat._worker.setup.is_gadget_turn", lambda _p: True)
    assert _task_draft_offered(turn(False, False)) is False


def test_build_specs_includes_draft_only_when_enabled() -> None:
    def names(**kw: Any) -> set[str]:
        return {
            s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, **kw)
        }

    assert TOOL_DRAFT_SCHEDULED_TASK not in names()
    assert TOOL_DRAFT_SCHEDULED_TASK in names(include_task_draft=True)


def test_assistant_metadata_carries_draft() -> None:
    st = SimpleNamespace(accumulated_reasoning="", reasoning_truncated=False)
    assert _assistant_metadata(st, [], None) == {}  # type: ignore[arg-type]
    draft = ScheduledTaskDraft.model_validate(VALID).model_dump(mode="json")
    meta = _assistant_metadata(st, [], None, task_draft=draft)  # type: ignore[arg-type]
    assert meta["metadata_json"]["task_draft"] == draft


def test_parse_task_draft_lenient() -> None:
    assert parse_task_draft(None) is None
    assert parse_task_draft({"task_draft": {"time": "99"}}) is None
    assert parse_task_draft({"task_draft": "x"}) is None
    parsed = parse_task_draft({"task_draft": VALID})
    assert parsed is not None and parsed.name == "Morning brief"


def test_draft_defaults_and_validation() -> None:
    d = ScheduledTaskDraft.model_validate({"kind": "daily_brief", "name": "n", "time": "06:30"})
    assert d.days == "daily" and d.sections == list(DRAFT_SECTIONS)
    with pytest.raises(ValidationError):
        ScheduledTaskDraft.model_validate({**VALID, "sections": []})


def test_draft_sections_match_dashboard() -> None:
    assert DRAFT_SECTIONS == tuple(dashboard_public.ALL_BRIEF_SECTIONS)
