"""Topic watch lifecycle: provisioning, one-time consent, edits, delete and orphan compensation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import ValidationFailedError
from core.workspaces.schemas import WorkspaceContext
from modules.automations import _topic_watch as lifecycle
from modules.automations import public
from modules.automations.schemas import AutomationCreate, topic_consent_hash

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
TRIGGER = {"type": "schedule", "cron": "0 8 * * *", "timezone": "UTC"}
WATCH = {
    "topic": "ai chips",
    "keep_rule": None,
    "languages": ["en"],
    "include_sites": [],
    "exclude_sites": [],
    "max_items": 5,
}
EST = 15600  # 600 + 5 * 2 * 1500, the same number the web editor pins
SOURCE = uuid4()


class Calls:
    """Records the connectors owner commands the lifecycle invokes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[Any, ...]] = []

        async def provision(_s: Any, *, name: str, config: Any, **_k: Any) -> Any:
            self.events.append(("provision", name, config.token_budget, config.model_role))
            return SOURCE

        async def update(_s: Any, source_id: Any, config: Any, **_k: Any) -> None:
            self.events.append(("update", source_id, config.topic))

        async def retire(_s: Any, source_id: Any, *, purge_items: bool, **_k: Any) -> None:
            self.events.append(("retire", source_id, purge_items))

        monkeypatch.setattr(lifecycle.connectors, "provision_topic_watch_source", provision)
        monkeypatch.setattr(lifecycle.connectors, "update_topic_watch_source", update)
        monkeypatch.setattr(lifecycle.connectors, "retire_topic_watch_source", retire)


def _action(**over: Any) -> dict[str, Any]:
    return {"type": "collect_source", "watch": WATCH, "model_role": "background", **over}


async def _prepare(
    action: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    allow: bool,
    trigger: dict[str, Any] = TRIGGER,
) -> tuple[dict[str, Any], bool]:
    return await lifecycle.prepare_topic_watch(
        None,  # type: ignore[arg-type]
        action,
        trigger,
        previous,
        name="Watch",
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        allow_consent=allow,
    )


@pytest.mark.asyncio
async def test_create_provisions_once_and_stores_consent_for_matching_estimate(monkeypatch) -> None:
    calls = Calls(monkeypatch)
    stored, created = await _prepare(_action(grant_consent_est_tokens=EST), allow=True)
    assert created and calls.events == [("provision", "Watch", 23400, "background")]
    assert stored["source_id"] == str(SOURCE) and "grant_consent_est_tokens" not in stored
    assert stored["consent"]["hash"] == topic_consent_hash(WATCH, TRIGGER, "background")
    assert stored["consent"]["est_tokens"] == EST


@pytest.mark.asyncio
async def test_mismatched_estimate_is_refused_before_any_source_exists(monkeypatch) -> None:
    calls = Calls(monkeypatch)
    with pytest.raises(ValidationFailedError):
        await _prepare(_action(grant_consent_est_tokens=EST - 1), allow=True)
    assert calls.events == []


@pytest.mark.asyncio
async def test_tool_path_never_stores_consent_and_client_values_are_ignored(monkeypatch) -> None:
    Calls(monkeypatch)
    forged = {"hash": "a" * 64, "granted_at": "2026-11-18T00:00:00+00:00", "est_tokens": EST}
    stored, _ = await _prepare(
        _action(grant_consent_est_tokens=EST, consent=forged, source_id=str(uuid4())), allow=False
    )
    assert "consent" not in stored and stored["source_id"] == str(SOURCE)


@pytest.mark.asyncio
async def test_editing_the_topic_clears_consent_unless_regranted(monkeypatch) -> None:
    calls = Calls(monkeypatch)
    previous = _action(source_id=str(SOURCE))
    previous["consent"] = {
        "hash": topic_consent_hash(WATCH, TRIGGER, "background"),
        "granted_at": "2026-11-18T00:00:00+00:00",
        "est_tokens": EST,
    }
    unchanged, created = await _prepare(_action(), previous, allow=True)
    assert not created and unchanged["consent"] == previous["consent"] and calls.events == []
    edited = {**WATCH, "topic": "ai chips and memory"}
    cleared, _ = await _prepare(_action(watch=edited), previous, allow=True)
    assert "consent" not in cleared and cleared["source_id"] == str(SOURCE)
    assert calls.events == [("update", SOURCE, "ai chips and memory")]
    regranted, _ = await _prepare(
        _action(watch=edited, grant_consent_est_tokens=EST), previous, allow=True
    )
    assert regranted["consent"]["hash"] == topic_consent_hash(edited, TRIGGER, "background")
    # A schedule change alone also invalidates consent (the hash covers cron and timezone).
    moved, _ = await _prepare(
        _action(), previous, allow=True, trigger={**TRIGGER, "cron": "0 9 * * *"}
    )
    assert "consent" not in moved


# ---------------------------------------------------------------- delete and orphan compensation
def _delete_patches(monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> None:
    head = SimpleNamespace(id=uuid4(), revision=1, enabled=True, deleted_at=None)
    cur = SimpleNamespace(trigger=TRIGGER, conditions=[], actions=[_action(source_id=str(SOURCE))])

    async def noop(*_a: Any, **_k: Any) -> Any:
        return None

    async def owned(*_a: Any, **_k: Any) -> Any:
        return head

    async def current(*_a: Any, **_k: Any) -> Any:
        return cur

    async def release(*_a: Any, **kwargs: Any) -> None:
        events.append(("release", kwargs["disable_internal"]))

    async def delete_thread(*_a: Any, **_k: Any) -> int:
        events.append(("thread",))
        return 1

    async def commit(*_a: Any, **_k: Any) -> None:
        events.append(("commit",))

    monkeypatch.setattr(public, "_admit", noop)
    monkeypatch.setattr(public, "_owned_head", owned)
    monkeypatch.setattr(public, "_current_revision", current)
    monkeypatch.setattr(public, "_append_revision", lambda *_a: None)
    monkeypatch.setattr(public.dashboard, "release_brief_slot", release)
    monkeypatch.setattr(public.chat, "delete_automation_conversation", delete_thread)
    monkeypatch.setattr(public, "commit_with_replay", commit)


@pytest.mark.asyncio
@pytest.mark.parametrize("purge", [False, True])
async def test_delete_archives_by_default_and_purges_on_request(monkeypatch, purge: bool) -> None:
    calls = Calls(monkeypatch)
    _delete_patches(monkeypatch, calls.events)
    await public.delete_automation(
        None,  # type: ignore[arg-type]
        uuid4(),
        1,
        purge_items=purge,
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )
    # The thread goes with the rule; the Source is retired only after the rule's own commit;
    # the internal brief cron is never disabled by a topic watch.
    assert calls.events == [
        ("release", False),
        ("thread",),
        ("commit",),
        ("retire", SOURCE, purge),
    ]


class _CreateSession:
    """Session double for ``create_automation``: no live rules, accepts adds and flushes."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def scalars(self, _statement: Any) -> Any:
        return SimpleNamespace(all=lambda: [])

    def add(self, _row: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_failure_after_provisioning_retires_the_orphan_source(monkeypatch) -> None:
    calls = Calls(monkeypatch)

    async def admit(*_a: Any, **_k: Any) -> str:
        return "fence"

    async def release(*_a: Any, **_k: Any) -> None:
        return None

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(public, "_admit", admit)
    monkeypatch.setattr(public, "validate_definition", lambda *_a: None)
    monkeypatch.setattr(
        public,
        "_append_revision",
        lambda *_a: SimpleNamespace(trigger=TRIGGER, conditions=[], actions=[]),
    )
    monkeypatch.setattr(public.dashboard, "release_brief_slot", release)
    monkeypatch.setattr(public, "commit_with_replay", boom)
    payload = AutomationCreate.model_validate(
        {
            "name": "Watch",
            "trigger": TRIGGER,
            "actions": [{"type": "collect_source", "watch": {"topic": "ai chips"}}],
        }
    )
    session = _CreateSession()
    with pytest.raises(RuntimeError, match="commit failed"):
        await public.create_automation(
            session,  # type: ignore[arg-type]
            payload,
            {},
            SimpleNamespace(),  # type: ignore[arg-type]
            allow_consent=True,
            scope=SCOPE,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
        )
    assert [e[0] for e in calls.events] == ["provision", "retire"]
    assert calls.events[1] == ("retire", SOURCE, False) and session.rolled_back
