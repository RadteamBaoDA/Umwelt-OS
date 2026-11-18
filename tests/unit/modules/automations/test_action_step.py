"""Characterization of ``_action_step``: row-state handling, durable pauses and dispatch ordering."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest

from modules.automations import execution
from tests.unit.split_patching import patch_split

RUN_ID = uuid4()
SCOPE = SimpleNamespace(workspace_id=uuid4(), actor_user_id=1, user_id=1)


class FakeSession:
    """Serve the action row then its revision, ignoring every other statement."""

    def __init__(self, row: Any, rev: Any) -> None:
        """Queue the two ``scalar`` results."""
        self.queue = [row, rev]

    async def scalar(self, _statement: Any) -> Any:
        """Return the next queued row."""
        return self.queue.pop(0)

    async def __aenter__(self) -> Self:
        """Enter the session scope."""
        return self

    async def __aexit__(self, *_a: object) -> None:
        """Leave the session scope."""


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "create_task",
    state: str = "pending",
    **over: Any,
) -> tuple[Any, list[str], dict[str, Any], Any]:
    """Patch every collaborator with an event-recording spy and return the call context."""
    events: list[str] = []
    row = SimpleNamespace(
        status=state,
        error_code=None,
        attempts=0,
        destination_revision=None,
        approved_session_hash="sess",
        approval_hash=None,
        approval_expires_at=None,
    )
    spec = {"type": kind, "alias": "hook"}
    rev = SimpleNamespace(actions=[spec], name="Rule")
    run = SimpleNamespace(id=RUN_ID, automation_id=uuid4(), revision=1)
    flags: dict[str, Any] = {
        "evidence": True,
        "fence": True,
        "modules": True,
        "dest_stale": False,
        "destination": "rev-1",
    }
    flags.update(over)

    async def spy(name: str, result: Any = None, *_a: Any, **_k: Any) -> Any:
        """Record an awaited collaborator call."""
        events.append(name)
        return result

    async def admit(*_a: Any, **_k: Any) -> str:
        """Return a stand-in fence."""
        events.append("admit")
        return "fence"

    async def locked_run(*_a: Any, **_k: Any) -> Any:
        """Return the locked run."""
        return run

    async def evidence(*_a: Any, **_k: Any) -> bool:
        """Answer the evidence-current probe."""
        return flags["evidence"]

    async def fence_current(*_a: Any, **_k: Any) -> bool:
        """Answer the revision fence probe."""
        return flags["fence"]

    async def modules(*_a: Any, **_k: Any) -> bool:
        """Answer the owner-module probe."""
        return flags["modules"]

    patch_split(monkeypatch, "modules.automations", "_admit", admit, facade="execution")
    patch_split(monkeypatch, "modules.automations", "_locked_run", locked_run, facade="execution")
    patch_split(
        monkeypatch, "modules.automations", "_run_evidence_current", evidence, facade="execution"
    )
    patch_split(
        monkeypatch, "modules.automations", "_fence_current", fence_current, facade="execution"
    )
    patch_split(
        monkeypatch, "modules.automations", "_action_modules_enabled", modules, facade="execution"
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_destination_stale",
        lambda *_a: flags["dest_stale"],
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "webhook_profile_revision",
        lambda *_a: flags["destination"],
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_approval_hash",
        lambda *_a: "approval-hash",
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_finish",
        lambda r, status, reason: events.append(f"finish:{status}:{reason}"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_skip_pending",
        lambda *_a: spy("skip_pending"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_commit",
        lambda *_a, **_k: spy("commit"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "emit",
        lambda *_a, **_k: spy("emit"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_in_database_action",
        lambda *_a, **_k: spy("in_database", "in-db"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_generate_brief",
        lambda *_a, **_k: spy("brief", "brief-done"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_start_agent",
        lambda *a, **_k: spy("start_agent:" + str(a[4]), "agent-done"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_send_webhook",
        lambda *a, **_k: spy("send_webhook:" + str(a[5]), "hook-done"),
        facade="execution",
    )
    ctx = {
        "session_factory": lambda: FakeSession(row, rev),
        "settings": SimpleNamespace(approval_expiry_hours=24),
    }
    return row, events, flags, ctx


async def _step(ctx: Any) -> str:
    """Advance action 1 of the shared run."""
    return await execution._action_step(ctx, RUN_ID, 1, scope=SCOPE, multi_workspace_enabled=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("denied", "failed"),
        ("skipped", "failed"),
        ("awaiting_approval", "awaiting_approval"),
    ],
)
async def test_terminal_and_waiting_states(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: str
) -> None:
    """Settled rows report their outcome without committing; awaiting approval stays parked."""
    row, events, _flags, ctx = _setup(monkeypatch, state=state)
    assert await _step(ctx) == expected
    assert "commit" not in events and row.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "result", "row_state", "code"),
    [
        ("in_flight", "requires_review", "requires_review", "document_evidence_unavailable"),
        ("pending", "dropped", "skipped", "document_evidence_unavailable"),
    ],
)
async def test_missing_evidence_settles_run(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    result: str,
    row_state: str,
    code: str,
) -> None:
    """Revoked evidence keeps uncertainty for dispatched rows and drops untouched ones."""
    row, events, _flags, ctx = _setup(monkeypatch, state=state, evidence=False)
    assert await _step(ctx) == result
    assert (row.status, row.error_code) == (row_state, code)
    assert events == ["admit", f"finish:{result}:{code}", "skip_pending", "commit"]


@pytest.mark.asyncio
async def test_ambiguous_restart_requires_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-agent in-flight row after a restart is review-only and never replayed."""
    row, events, _flags, ctx = _setup(monkeypatch, kind="call_webhook", state="in_flight")
    assert await _step(ctx) == "requires_review"
    assert (row.status, row.error_code) == ("requires_review", "ambiguous_after_restart")
    assert events[-1] == "commit"


@pytest.mark.asyncio
async def test_stale_revision_destination_and_paused_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence, module and destination drift drop or pause the action without dispatching."""
    row, events, _f, ctx = _setup(monkeypatch, fence=False)
    assert await _step(ctx) == "dropped" and (row.status, row.error_code) == (
        "skipped",
        "stale_revision",
    )
    row, events, _f, ctx = _setup(monkeypatch, modules=False)
    assert await _step(ctx) == "paused" and row.status == "pending" and "commit" not in events
    row, events, _f, ctx = _setup(
        monkeypatch, kind="call_webhook", state="approved", dest_stale=True
    )
    assert await _step(ctx) == "dropped" and row.error_code == "stale_destination"


@pytest.mark.asyncio
async def test_webhook_needs_approval_then_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending webhook binds its destination revision, hashes the request and notifies the owner."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook")
    assert await _step(ctx) == "awaiting_approval"
    assert (row.status, row.destination_revision, row.approval_hash) == (
        "awaiting_approval",
        "rev-1",
        "approval-hash",
    )
    assert row.approval_expires_at is not None and events[-2:] == ["emit", "commit"]


@pytest.mark.asyncio
async def test_webhook_without_profile_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing deployment profile fails the action instead of asking for approval."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook", destination=None)
    assert (
        await _step(ctx) == "failed"
        and row.error_code == "webhook_unavailable"
        and "emit" not in events
    )


@pytest.mark.asyncio
async def test_in_database_actions_run_inside_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification and task actions count an attempt and are delegated with the open session."""
    row, events, _f, ctx = _setup(monkeypatch, kind="create_task")
    assert await _step(ctx) == "in-db" and row.attempts == 1 and events == ["admit", "in_database"]


@pytest.mark.asyncio
async def test_generate_brief_commits_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brief generation releases the locks first because it commits internally."""
    row, events, _f, ctx = _setup(monkeypatch, kind="generate_brief")
    assert (
        await _step(ctx) == "brief-done"
        and row.attempts == 1
        and events == ["admit", "commit", "brief"]
    )


@pytest.mark.asyncio
async def test_run_agent_marks_in_flight_then_starts_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of no return is committed before the idempotent agent start, keeping the digest."""
    row, events, _f, ctx = _setup(monkeypatch, kind="run_agent", state="in_flight")
    assert await _step(ctx) == "agent-done"
    assert row.status == "in_flight" and row.approved_session_hash == "sess"
    assert events == ["admit", "commit", "start_agent:sess"]


@pytest.mark.asyncio
async def test_approved_webhook_clears_digest_and_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved webhook drops the stored session digest before the post-commit send."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook", state="approved")
    row.destination_revision = "rev-1"
    assert await _step(ctx) == "hook-done"
    assert row.approved_session_hash is None and row.status == "in_flight"
    assert events == ["admit", "commit", "send_webhook:rev-1"]


# ---------------------------------------------------------------- daily brief scheduled task
class _BriefFactory:
    """Session factory whose sessions only answer the run lookup."""

    def __call__(self) -> Self:
        """Return a session scope."""
        return self

    async def __aenter__(self) -> Self:
        """Enter the session scope."""
        return self

    async def __aexit__(self, *_a: object) -> None:
        """Leave the session scope."""

    async def scalar(self, _statement: Any) -> Any:
        """Serve the run row."""
        return SimpleNamespace(id=RUN_ID)

    async def rollback(self) -> None:
        """No-op rollback."""


def _brief_harness(
    monkeypatch: pytest.MonkeyPatch, briefs: list[str], *, fail_first_commit: bool = False
) -> tuple[dict[str, Any], list[Any], list[tuple[Any, ...]]]:
    """Patch ``_generate_brief`` collaborators; return (message store, calls, marks)."""
    from modules.automations._execution import actions

    store: dict[str, Any] = {}
    appended: list[Any] = []
    marks: list[tuple[Any, ...]] = []
    state = {"failed": False}

    async def admit(*_a: Any, **_k: Any) -> str:
        return "fence"

    async def true(*_a: Any, **_k: Any) -> bool:
        return True

    async def commit(*_a: Any, **_k: Any) -> None:
        if fail_first_commit and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("boom after append")

    async def mark(
        _f: Any, _run: Any, _ord: Any, status: str, _c: Any, ref: Any, **_k: Any
    ) -> None:
        marks.append((status, ref))

    async def transient(*_a: Any, **_k: Any) -> str:
        return "retry"

    async def read_schedule(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(timezone="UTC")

    async def generate(*_a: Any, **kwargs: Any) -> Any:
        appended.append(("generate", kwargs.get("sections"), kwargs.get("extra_instruction")))
        if not briefs:
            raise actions.dashboard.BriefEmpty()
        return SimpleNamespace(
            id=briefs[0], revision=1, content="Hi", citations=[{"ref": "f1", "source_ids": []}]
        )

    async def ensure(*_a: Any, **_k: Any) -> Any:
        return "conv"

    async def append(_s: Any, _c: Any, *, dedupe_key: str, **kwargs: Any) -> tuple[Any, bool]:
        appended.append(("append", dedupe_key, kwargs["metadata"]))
        created = dedupe_key not in store
        store.setdefault(dedupe_key, kwargs["content"])
        return uuid4(), created

    for name, fn in [
        ("_admit", admit),
        ("_action_modules_enabled", true),
        ("_run_evidence_current", true),
        ("_commit", commit),
        ("_mark", mark),
        ("_transient", transient),
    ]:
        monkeypatch.setattr(actions, name, fn)
    monkeypatch.setattr(actions.dashboard, "read_schedule", read_schedule)
    monkeypatch.setattr(actions.dashboard, "generate_brief", generate)
    monkeypatch.setattr(actions.chat, "ensure_automation_conversation", ensure)
    monkeypatch.setattr(actions.chat, "append_task_message", append)
    return store, appended, marks


def _brief_rev(**spec: Any) -> Any:
    """A schedule rule revision with one delivering brief action."""
    action = {"type": "generate_brief", "deliver_chat": True, **spec}
    trigger = {"type": "schedule", "cron": "0 7 * * *", "timezone": "UTC"}
    return SimpleNamespace(name="Morning", trigger=trigger, actions=[action])


async def _run_brief(rev: Any) -> str:
    """Run ``_generate_brief`` for action 1 of the shared run."""
    from modules.automations._execution import actions

    ctx = {"session_factory": _BriefFactory(), "settings": object(), "redis": object()}
    run = SimpleNamespace(id=RUN_ID, automation_id=uuid4())
    row = SimpleNamespace(ordinal=1, action_type="generate_brief")
    return await actions._generate_brief(
        ctx,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        row,  # type: ignore[arg-type]
        rev,
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )


@pytest.mark.asyncio
async def test_brief_task_posts_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    store, calls, marks = _brief_harness(monkeypatch, ["B"])
    assert await _run_brief(_brief_rev(sections=["stories"], extra_instruction="x")) == "succeeded"
    assert list(store) == ["brief:B"]
    assert ("generate", ["stories"], "x") in calls
    meta = next(c[2] for c in calls if c[0] == "append")
    assert meta["brief_id"] == "B" and meta["task_kind"] == "daily_brief"
    assert marks == [("succeeded", "brief:B")]


@pytest.mark.asyncio
async def test_old_spec_without_deliver_chat_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _calls, marks = _brief_harness(monkeypatch, ["B"])
    assert await _run_brief(_brief_rev(deliver_chat=False)) == "succeeded"
    assert store == {} and marks == [("succeeded", "brief:B")]


@pytest.mark.asyncio
async def test_retry_posts_brief_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _calls, _marks = _brief_harness(monkeypatch, ["B"], fail_first_commit=True)
    assert await _run_brief(_brief_rev()) == "retry"
    assert await _run_brief(_brief_rev()) == "succeeded"
    assert list(store) == ["brief:B"]


@pytest.mark.asyncio
async def test_reused_brief_posts_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _calls, marks = _brief_harness(monkeypatch, ["B"])
    await _run_brief(_brief_rev())
    await _run_brief(_brief_rev())
    assert list(store) == ["brief:B"] and len(marks) == 2


@pytest.mark.asyncio
async def test_brief_empty_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _calls, marks = _brief_harness(monkeypatch, [])
    assert await _run_brief(_brief_rev()) == "succeeded"
    assert store == {} and marks == [("succeeded", "brief:empty")]


@pytest.mark.asyncio
async def test_second_brief_task_refused_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second enabled brief rule: BriefSlotOwned -> AutomationConflict -> HTTP 409 brief_slot_owned."""
    from fastapi import HTTPException

    from modules.automations import public, routes

    async def owned(*_a: Any, **_k: Any) -> None:
        raise public.dashboard.BriefSlotOwned()

    monkeypatch.setattr(public.dashboard, "claim_brief_slot", owned)
    head = SimpleNamespace(id=uuid4(), enabled=True)
    parts = ({"type": "schedule"}, [], [{"type": "generate_brief"}])
    with pytest.raises(public.AutomationConflict) as caught:
        await public._sync_brief_slot(
            None,  # type: ignore[arg-type]
            head,  # type: ignore[arg-type]
            parts,
            explicit_enable=True,
            was_owner_candidate=False,
            scope=SCOPE,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
        )

    async def raising() -> None:
        raise caught.value

    with pytest.raises(HTTPException) as http:
        await routes._call(raising())
    assert http.value.status_code == 409
    assert http.value.detail["code"] == "brief_slot_owned"  # type: ignore[index]


@pytest.mark.asyncio
async def test_next_run_at_in_read() -> None:
    from datetime import UTC, datetime

    from modules.automations import public

    slot = datetime(2030, 1, 1, 7, tzinfo=UTC)

    class Sess:
        async def scalar(self, _s: Any) -> Any:
            return slot

    now = datetime(2030, 1, 1, tzinfo=UTC)
    head = SimpleNamespace(
        id=uuid4(), name="n", enabled=True, revision=2, created_at=now, updated_at=now
    )
    rev = SimpleNamespace(
        trigger={"type": "schedule", "cron": "0 7 * * *", "timezone": "UTC"},
        conditions=[],
        actions=[],
    )
    assert (await public._read(Sess(), head, rev)).next_run_at == slot  # type: ignore[arg-type]
    head.enabled = False
    assert (await public._read(Sess(), head, rev)).next_run_at is None  # type: ignore[arg-type]
    head.enabled = True
    rev.trigger = {"type": "webhook"}
    assert (await public._read(Sess(), head, rev)).next_run_at is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_brief_task_deletes_thread_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.automations import public

    events: list[str] = []
    thread = uuid4()
    head = SimpleNamespace(id=uuid4(), revision=1, enabled=True, deleted_at=None)
    cur = SimpleNamespace(
        trigger={"type": "schedule", "cron": "0 7 * * *", "timezone": "UTC"},
        conditions=[],
        actions=[{"type": "generate_brief", "deliver_chat": True}],
    )

    async def admit(*_a: Any, **_k: Any) -> str:
        return "fence"

    async def owned(*_a: Any, **_k: Any) -> Any:
        return head

    async def current(*_a: Any, **_k: Any) -> Any:
        return cur

    async def release(*_a: Any, **_k: Any) -> None:
        events.append("release")

    async def conv(*_a: Any, **_k: Any) -> Any:
        return thread

    async def delete(_s: Any, cid: Any, owner: Any) -> bool:
        events.append(f"delete:{cid == thread}:{owner}")
        return True

    async def commit(*_a: Any, **_k: Any) -> None:
        events.append("commit")

    monkeypatch.setattr(public, "_admit", admit)
    monkeypatch.setattr(public, "_owned_head", owned)
    monkeypatch.setattr(public, "_current_revision", current)
    monkeypatch.setattr(public, "_append_revision", lambda *_a: None)
    monkeypatch.setattr(public.dashboard, "release_brief_slot", release)
    monkeypatch.setattr(public, "_conversation_id", conv)
    monkeypatch.setattr(public, "delete_conversation", delete)
    monkeypatch.setattr(public, "commit_with_replay", commit)
    await public.delete_automation(
        None,  # type: ignore[arg-type]
        head.id,
        1,
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )
    assert events == ["release", "delete:True:1", "commit"]
