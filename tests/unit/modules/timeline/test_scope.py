"""Workspace-scope contracts for Timeline: member denial, scoped predicates, seed and worker gating."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.timeline import public, worker
from modules.timeline.models import Event
from modules.timeline.schemas import TimelineQuery
from modules.timeline.seed import ensure_demo_events

OWNER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=OWNER.workspace_id, role="member", membership_revision=1)
KW = {"scope": OWNER, "multi_workspace_enabled": False}
MKW = {"scope": MEMBER, "multi_workspace_enabled": False}


def _sql(statement: object) -> tuple[str, list[object]]:
    """Compile a statement so scope predicates and bound values can be asserted."""
    compiled = statement.compile()  # type: ignore[attr-defined]
    return str(compiled), list(compiled.params.values())


def _admitted() -> object:
    """Skip the workspace fence lookup; the owner-role gate is tested separately."""
    return patch("modules.timeline.public.workspaces.read_access_fence", AsyncMock(return_value=MagicMock()))


@pytest.mark.asyncio
async def test_member_without_share_sees_nothing() -> None:
    """Every read entry point rejects a member before touching a row."""
    session = AsyncMock()
    calls = [
        public.get_event(session, uuid4(), **MKW),
        public.list_event_evidence(session, uuid4(), **MKW),
        public.list_events(session, **MKW),
        public.list_timeline(session, TimelineQuery(), **MKW),
        public.temporal_event_refs(session, [uuid4()], **MKW),
        public.lock_event_ids(session, [uuid4()], **MKW),
        public.list_changed_events_after(session, None, 10, **MKW),
        public.export_page(session, owner_id=8, record_kind="events", **MKW),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 403
    session.scalar.assert_not_called()
    session.scalars.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_event_id_is_absent_and_query_is_workspace_scoped() -> None:
    """An event id from another workspace reads as absent; the SELECT carries the workspace."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    with _admitted():
        assert await public.get_event(session, uuid4(), **KW) is None
    text, params = _sql(session.scalar.call_args.args[0])
    assert "timeline_events.workspace_id" in text
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_change_feed_filters_workspace_before_limit() -> None:
    """The automation change feed is workspace-scoped ahead of ORDER BY / LIMIT."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    with _admitted():
        assert await public.list_changed_events_after(session, None, 5, **KW) == []
    text, params = _sql(session.scalars.call_args.args[0])
    assert text.index("timeline_events.workspace_id") < text.index("ORDER BY") < text.index("LIMIT")
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_export_rejects_other_actor() -> None:
    """An export for an owner id that is not the scope's actor is refused."""
    with _admitted(), pytest.raises(PermissionError):
        await public.export_page(AsyncMock(), owner_id=999, record_kind="events", **KW)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [OWNER, InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=7, membership_revision=1)])
async def test_demo_seed_carries_workspace_id(scope: WorkspaceContext | InternalJobScope) -> None:
    """The seeded event is stamped with the workspace and skipped when that workspace already has it."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    with patch("modules.timeline.seed.workspaces.read_access_fence", AsyncMock()):
        assert await ensure_demo_events(session, scope=scope, multi_workspace_enabled=False) == (1, 0)
        event = session.add.call_args.args[0]
        assert isinstance(event, Event)
        assert event.workspace_id == OWNER.workspace_id
        session.scalar = AsyncMock(return_value=event.id)
        assert await ensure_demo_events(session, scope=scope, multi_workspace_enabled=False) == (0, 1)


@pytest.mark.asyncio
async def test_disabled_module_leaves_work_untouched() -> None:
    """A workspace that disabled the module is neither claimed nor acknowledged by the worker."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=OWNER.workspace_id)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    ctx = {"session_factory": factory, "redis": MagicMock(),
           "settings": MagicMock(multi_workspace_enabled=False)}
    job_scope = InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=7, membership_revision=1)
    with (
        patch.object(worker, "_workspace_job_scope", AsyncMock(return_value=job_scope)),
        patch.object(worker.settings_public, "module_is_enabled", AsyncMock(return_value=False)) as gate,
        patch.object(worker.timeline, "claim_extraction_work", AsyncMock()) as claim,
    ):
        # unwrap the heavy-slot guard: it needs a live Redis-backed slot, the gate runs before it matters
        await worker.process_timeline_extraction_work.__wrapped__(ctx, str(uuid4()))
    gate.assert_awaited_once()
    claim.assert_not_called()


OTHER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)


def test_export_cursor_is_bound_to_workspace() -> None:
    """A cursor minted in one workspace is rejected in another even for the same owner."""
    from datetime import UTC, datetime, timedelta
    at = datetime.now(UTC) - timedelta(minutes=1)
    cursor = public._encode_timeline_export_cursor(7, OWNER.workspace_id, at, at, uuid4())
    public._decode_timeline_export_cursor(cursor, 7, OWNER.workspace_id)
    with pytest.raises(ValueError):
        public._decode_timeline_export_cursor(cursor, 7, OTHER.workspace_id)


@pytest.mark.asyncio
async def test_correction_event_ids_denies_member_before_query() -> None:
    """The public correction read applies the role-only owner guard without any I/O."""
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await public.correction_event_ids(session, [uuid4()], scope=MEMBER)
    assert caught.value.status_code == 403
    session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_event_routes_do_not_pass_route_actor() -> None:
    """Event audit actor derives from the scope, so routes no longer forward an owner id."""
    from modules.timeline import routes
    request = MagicMock()
    request.app.state.settings.multi_workspace_enabled = False
    for name, call in (
        ("create_event", lambda: routes.create_event(MagicMock(), AsyncMock(), OWNER, request, MagicMock())),
        ("update_event", lambda: routes.update_event(uuid4(), MagicMock(), AsyncMock(), OWNER, request, MagicMock())),
        ("delete_event", lambda: routes.delete_event(uuid4(), AsyncMock(), OWNER, request, MagicMock(), 1, "r")),
    ):
        with patch.object(routes.public, name, AsyncMock(return_value=True)) as target:
            await call()
        assert "actor_id" not in target.call_args.kwargs
