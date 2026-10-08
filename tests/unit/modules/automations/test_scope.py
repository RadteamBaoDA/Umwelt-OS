"""Workspace-scope contracts for Automations: member denial, scoped predicates, worker discovery, webhook ingress."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core import worker_cursors
from core.workspaces.schemas import WorkspaceContext
from modules.automations import execution, producers, public, routes, tools, worker
from modules.automations.models import AutomationCursor
from modules.automations.schemas import AutomationCreate, AutomationUpdate, PreviewRequest
from modules.chat.public import Conversation
from modules.tools.mcp_credentials import hash_inbound_token

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=1)
FLAG = False
SC = {"scope": OWNER, "multi_workspace_enabled": FLAG}


def _sql(statement: object) -> tuple[str, list[object]]:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled), list(compiled.params.values())


def _factory(session: object) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _session(ids: list[UUID] | None = None) -> AsyncMock:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=ids or [])
    return session


# ---------------------------------------------------------------- member denial before any query
@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda s: public.create_automation(s, AutomationCreate(name="x", trigger={"type": "schedule", "cron": "0 8 * * 1", "timezone": "UTC"}, actions=[{"type": "create_notification", "message": "m"}]), {}, MagicMock(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.update_automation(s, uuid4(), AutomationUpdate(expected_revision=1), {}, MagicMock(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.delete_automation(s, uuid4(), 1, scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.get_automation(s, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.list_automations(s, scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.get_revision(s, uuid4(), 1, scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.get_automation_conversation_id(s, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: public.preview(s, MagicMock(spec=PreviewRequest), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: execution.start_manual(s, uuid4(), 1, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: execution.list_runs(s, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: execution.decide_action(s, "h", uuid4(), 1, True, MagicMock(), scope=MEMBER, multi_workspace_enabled=FLAG),
    lambda s: execution.enqueue_trigger(s, "webhook", "k", {"event": "e"}, hook="h", scope=MEMBER, multi_workspace_enabled=FLAG),
])
async def test_member_denied_before_any_query(call) -> None:
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await call(session)
    assert caught.value.status_code == 403
    for name in ("execute", "scalar", "scalars", "get", "flush", "commit"):
        getattr(session, name).assert_not_called()


# ---------------------------------------------------------------- predicates before LIMIT
@pytest.mark.asyncio
async def test_live_rules_workspace_and_owner_before_limit() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=list))))
    await execution.live_rules(session, "task_due", scope=OWNER)
    text, params = _sql(session.execute.call_args.args[0])
    assert text.index("automations.workspace_id") < text.index("automations.owner_id") < text.index("LIMIT")
    assert WS in params and OWNER.user_id in params and 100 in params


@pytest.mark.asyncio
async def test_dispatch_runs_scopes_runs_before_limit() -> None:
    statements: list[object] = []
    session = AsyncMock()

    async def scalars(stmt: object) -> MagicMock:
        statements.append(stmt)
        return MagicMock(all=list)

    session.scalars = scalars
    with patch("modules.automations.execution._admit", AsyncMock(return_value=MagicMock())), \
         patch("modules.automations.execution.commit_with_replay", AsyncMock()):
        await execution.dispatch_runs(_factory(session), AsyncMock(), **SC)
    assert len(statements) == 2
    for stmt in statements:
        text, params = _sql(stmt)
        assert text.index("automation_runs.workspace_id") < text.index("LIMIT")
        assert "automation_runs.owner_id" in text and WS in params


# ---------------------------------------------------------------- worker discovery and cursor
def _owner_ctx() -> MagicMock:
    return MagicMock(user_id=OWNER.user_id, membership_revision=1)


def _worker_patches(**overrides: object):
    stack = {
        "modules.automations.worker.workspaces.resolve_workspace_owner_context": AsyncMock(return_value=_owner_ctx()),
        "modules.settings.public.module_is_enabled": AsyncMock(return_value=True),
        "modules.automations.worker.scheduler.tick": AsyncMock(return_value=0),
        "modules.automations.worker.producers.sweep": AsyncMock(return_value=0),
        "modules.automations.worker.execution.fan_out_triggers": AsyncMock(return_value=0),
        "modules.automations.worker.execution.dispatch_runs": AsyncMock(return_value=1),
    }
    stack.update(overrides)  # type: ignore[arg-type]
    return stack


def _enter(patches: dict[str, object]):
    from contextlib import ExitStack
    es = ExitStack()
    mocks = {name: es.enter_context(patch(name, mock)) for name, mock in patches.items()}
    return es, mocks


def _ctx(session: object) -> dict[str, object]:
    return {
        "session_factory": _factory(session), "settings": SimpleNamespace(multi_workspace_enabled=FLAG),
        "redis": MagicMock(), worker_cursors.STATE_KEY: {},
    }


@pytest.mark.asyncio
async def test_discovery_sql_is_distinct_ordered_and_limited() -> None:
    session = _session([uuid4()])
    es, _ = _enter(_worker_patches())
    with es:
        await worker.reconcile_automation_runs(_ctx(session))
    text, params = _sql(session.scalars.call_args.args[0])
    assert text.startswith("SELECT DISTINCT automations.workspace_id")
    assert "automations.deleted_at IS NULL" in text and "automations.enabled" not in text
    assert text.index("ORDER BY automations.workspace_id") < text.index("LIMIT")
    assert 100 in params


@pytest.mark.asyncio
async def test_cursor_persists_across_ctx_copies_and_wraps() -> None:
    first, second, third = sorted(uuid4() for _ in range(3))
    pages = [[first, second], [third]]
    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=lambda _stmt: pages.pop(0))
    base = {
        "session_factory": _factory(session), "settings": SimpleNamespace(multi_workspace_enabled=FLAG),
        "redis": None, worker_cursors.STATE_KEY: {},
    }
    es, _ = _enter(_worker_patches())
    with es, patch.object(worker, "PAGE", 2):
        await worker.reconcile_automation_runs(dict(base))  # full page: cursor advances to the last id
        assert base[worker_cursors.STATE_KEY][worker.CURSOR_KEY] == str(second)
        await worker.reconcile_automation_runs(dict(base))  # a second shallow copy sees the same cursor
        stmt_text, params = _sql(session.scalars.call_args.args[0])
        assert "automations.workspace_id >" in stmt_text and second in params
        assert base[worker_cursors.STATE_KEY][worker.CURSOR_KEY] == ""  # short page wraps


@pytest.mark.asyncio
async def test_owner_mismatch_skips_workspace_without_sweeps() -> None:
    session = _session([WS])
    patches = _worker_patches(**{
        "modules.automations.worker.workspaces.resolve_workspace_owner_context": AsyncMock(return_value=None)})
    es, mocks = _enter(patches)
    with es:
        assert await worker.reconcile_automation_runs(_ctx(session)) == 0
    for name in ("scheduler.tick", "producers.sweep", "execution.fan_out_triggers", "execution.dispatch_runs"):
        mocks[f"modules.automations.worker.{name}"].assert_not_called()


@pytest.mark.asyncio
async def test_denial_in_one_workspace_does_not_stop_the_next() -> None:
    first, second = sorted(uuid4() for _ in range(2))
    session = _session([first, second])
    calls: list[UUID] = []

    async def tick(_factory: object, *, scope: object, multi_workspace_enabled: bool) -> int:
        calls.append(scope.workspace_id)  # type: ignore[attr-defined]
        if scope.workspace_id == first:  # type: ignore[attr-defined]
            raise HTTPException(status_code=403, detail="Workspace owner required")
        return 0

    es, mocks = _enter(_worker_patches(**{"modules.automations.worker.scheduler.tick": tick}))
    with es:
        assert await worker.reconcile_automation_runs(_ctx(session)) == 1
    assert calls == [first, second]
    sweep = mocks["modules.automations.worker.producers.sweep"]
    assert [c.kwargs["scope"].workspace_id for c in sweep.call_args_list] == [second]


@pytest.mark.asyncio
async def test_non_denial_http_error_propagates() -> None:
    session = _session([WS])
    boom = AsyncMock(side_effect=HTTPException(status_code=500, detail="x"))
    es, _ = _enter(_worker_patches(**{"modules.automations.worker.scheduler.tick": boom}))
    with es, pytest.raises(HTTPException):
        await worker.reconcile_automation_runs(_ctx(session))


# ---------------------------------------------------------------- producers
@pytest.mark.asyncio
async def test_cursor_sweep_keys_by_workspace_and_passes_scope_to_readers() -> None:
    now = datetime.now(UTC)
    cursor = SimpleNamespace(ts=now - timedelta(minutes=5), item_id=None)
    session = AsyncMock()
    session.get = AsyncMock(return_value=cursor)
    session.scalar = AsyncMock(return_value=cursor)
    reader = AsyncMock(return_value=[])
    with patch("modules.automations.producers.execution.live_rules", AsyncMock(return_value=[MagicMock()])):
        await producers._cursor_sweep(
            session, "new_event", reader, now, access_fence=MagicMock(), **SC)
    assert session.get.call_args.args == (AutomationCursor, (WS, "new_event"))
    text, params = _sql(session.scalar.call_args.args[0])
    assert "automation_cursors.workspace_id" in text and WS in params and "new_event" in params
    for call in reader.call_args_list:
        position = call.args[1]
        assert isinstance(position, tuple) and isinstance(position[0], datetime) and isinstance(position[1], UUID)
        assert call.kwargs == {"scope": OWNER, "multi_workspace_enabled": FLAG}


@pytest.mark.asyncio
async def test_first_seen_cursor_is_inserted_with_workspace() -> None:
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    assert await producers._cursor_sweep(
        session, "new_event", AsyncMock(), datetime.now(UTC), access_fence=MagicMock(), **SC) == 0
    added = session.add.call_args.args[0]
    assert isinstance(added, AutomationCursor) and added.workspace_id == WS and added.name == "new_event"


# ---------------------------------------------------------------- inbound webhook
def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {"content-type": "application/json", "content-length": "10"}
    request.app.state.settings = SimpleNamespace(multi_workspace_enabled=FLAG)
    return request


def _credential(token: str, owner_id: int = OWNER.user_id) -> SimpleNamespace:
    return SimpleNamespace(workspace_id=WS, owner_id=owner_id, alias="hook", token_hash=hash_inbound_token(token))


async def _receive(session: object) -> object:
    return await routes.receive_inbound_webhook(
        "hook", _request(), session, MagicMock(headers={}), token="secret", event_key="evt-1")


@pytest.mark.asyncio
async def test_inbound_selects_by_alias_and_token_digest() -> None:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    with pytest.raises(HTTPException) as caught:
        await _receive(session)
    assert caught.value.status_code == 401
    text, params = _sql(session.scalars.call_args.args[0])
    assert "automation_webhook_credentials.alias" in text and "automation_webhook_credentials.token_hash" in text
    assert hash_inbound_token("secret") in params and "hook" in params
    assert "owner_id" not in text.split("WHERE")[1] and 1 not in params  # no literal owner 1


@pytest.mark.asyncio
async def test_inbound_two_matches_is_unauthorized() -> None:
    session = AsyncMock()
    rows = [_credential("secret"), _credential("secret")]
    session.scalars = AsyncMock(return_value=MagicMock(all=lambda: rows))
    with pytest.raises(HTTPException) as caught:
        await _receive(session)
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_inbound_owner_mismatch_is_unauthorized() -> None:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=lambda: [_credential("secret")]))
    other = MagicMock(user_id=OWNER.user_id + 1, membership_revision=1)
    with patch("modules.automations.routes.workspaces.resolve_workspace_owner_context", AsyncMock(return_value=other)), \
         pytest.raises(HTTPException) as caught:
        await _receive(session)
    assert caught.value.status_code == 401


# ---------------------------------------------------------------- T-F2 and tools
@pytest.mark.asyncio
async def test_automation_conversation_insert_carries_workspace_and_actor() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    await execution._automation_conversation(session, uuid4(), "Rule", scope=OWNER)
    text, params = _sql(session.scalar.call_args.args[0])
    assert "chat_conversations.workspace_id" in text and WS in params
    created = session.add.call_args.args[0]
    assert isinstance(created, Conversation)
    assert created.workspace_id == WS and created.actor_user_id == OWNER.user_id


@pytest.mark.asyncio
async def test_create_tool_perform_takes_scope_and_forwards_it() -> None:
    settings = SimpleNamespace(multi_workspace_enabled=FLAG)
    context = {"settings": settings, "principal": SimpleNamespace(scope=OWNER), "session_factory": MagicMock()}
    arguments = {
        "name": "n", "trigger": {"type": "schedule", "cron": "0 8 * * 1", "timezone": "UTC"},
        "actions": [{"type": "create_notification", "message": "m"}],
    }
    captured: dict[str, object] = {}

    async def fake_write(_args: object, _ctx: object, perform: object, _errors: object) -> object:
        captured["perform"] = perform
        return "result"

    with patch("modules.agents.internal_writes.run_approved_write", fake_write):
        await tools._create(arguments, context)
    perform = captured["perform"]
    assert list(inspect.signature(perform).parameters) == ["session", "scope"]  # type: ignore[arg-type]
    create = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    with patch.object(public, "create_automation", create):
        reference = await perform(AsyncMock(), OWNER)  # type: ignore[operator]
    assert reference.startswith("automation:")
    assert create.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": FLAG}


@pytest.mark.asyncio
async def test_list_tool_uses_principal_scope() -> None:
    session = AsyncMock()
    factory = _factory(session)
    context = {
        "settings": SimpleNamespace(multi_workspace_enabled=FLAG), "principal": SimpleNamespace(scope=OWNER),
        "session_factory": factory,
    }
    listing = AsyncMock(return_value=SimpleNamespace(items=[]))
    with patch.object(public, "list_automations", listing):
        result = await tools._list({}, context)
    assert result.success
    assert listing.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": FLAG}


# ---------------------------------------------------------------- process_run never rebases
@pytest.mark.asyncio
async def test_process_run_owner_mismatch_leaves_row_untouched() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(WS, OWNER.user_id))))
    other = MagicMock(user_id=OWNER.user_id + 1, membership_revision=1)
    ctx = {"session_factory": _factory(session), "settings": SimpleNamespace(multi_workspace_enabled=FLAG)}
    with patch("modules.automations.execution.workspaces.resolve_workspace_owner_context", AsyncMock(return_value=other)):
        assert await execution.process_run(ctx, str(uuid4())) == "noop"
    session.scalar.assert_not_called()
    session.commit.assert_not_called()


def test_no_literal_owner_one_remains() -> None:
    for module in (execution, producers, public, routes, worker):
        source = inspect.getsource(module)
        assert "OWNER_ID" not in source and "owner_id == 1" not in source and "owner_id=1" not in source
