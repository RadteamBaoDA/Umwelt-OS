"""Workspace-scope contracts for notifications: member denial, predicates, insert stamping, call order."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.notifications import public
from modules.notifications.schemas import NotificationEmit, NotificationEvidence

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=1)
FLAG = False
FENCE = AccessFence(WS, 7, 1, 1)


def _sql(statement: object) -> tuple[str, dict[str, object]]:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled), dict(compiled.params)


def _fences() -> object:
    """Stand in for workspace admission (no DB) while keeping the role check under test."""
    read = patch("modules.notifications.public.workspaces.read_access_fence", AsyncMock(return_value=FENCE))
    lock = patch("modules.notifications.public.workspaces.lock_access_fence", AsyncMock(return_value=FENCE))
    return _Both(read, lock)


class _Both:
    def __init__(self, *managers: object) -> None:
        self.managers = managers

    def __enter__(self) -> None:
        for manager in self.managers:
            manager.__enter__()  # type: ignore[attr-defined]

    def __exit__(self, *exc: object) -> None:
        for manager in reversed(self.managers):
            manager.__exit__(*exc)  # type: ignore[attr-defined]


def _session() -> AsyncMock:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    session.scalar = AsyncMock(return_value=0)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["emit", "list", "set_read"])
async def test_member_denied_before_any_query(call: str) -> None:
    session = _session()
    with pytest.raises(HTTPException) as caught:
        if call == "emit":
            await public.emit(
                session, NotificationEmit(dedupe_key="k", kind="x"), scope=MEMBER, multi_workspace_enabled=FLAG,
            )
        elif call == "list":
            await public.list_notifications(session, scope=MEMBER, multi_workspace_enabled=FLAG)
        else:
            await public.set_read(session, uuid4(), True, scope=MEMBER, multi_workspace_enabled=FLAG)
    assert caught.value.status_code == 403
    for awaited in (session.execute, session.scalar, session.scalars):
        awaited.assert_not_called()


@pytest.mark.asyncio
async def test_list_page_and_count_are_workspace_and_owner_scoped() -> None:
    session = _session()
    with _fences():
        await public.list_notifications(session, scope=OWNER, multi_workspace_enabled=FLAG)
    page, _ = _sql(session.scalars.call_args.args[0])
    count, _ = _sql(session.scalar.call_args.args[0])
    assert page.index("notifications.workspace_id =") < page.index("LIMIT")
    assert page.index("notifications.owner_id =") < page.index("LIMIT")
    assert "notifications.workspace_id =" in count and "notifications.owner_id =" in count


@pytest.mark.asyncio
async def test_emit_insert_carries_workspace_and_actor_with_json_safe_values() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: uuid4()))
    payload = NotificationEmit(dedupe_key="brief:1", kind="brief.ready", params={"date": "2026-10-08", "n": 3})
    with _fences():
        assert await public.emit(session, payload, scope=OWNER, multi_workspace_enabled=FLAG) is True
    text, params = _sql(session.execute.call_args.args[0])
    assert "workspace_id" in text and "owner_id" in text
    assert WS in params.values() and OWNER.user_id in params.values()
    assert params["params"] == {"date": "2026-10-08", "n": 3}
    json.dumps(params["params"])
    json.dumps(payload.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_emit_with_evidence_call_order_and_admitted_fence() -> None:
    version_id, document_id, definition_id, rule_id = uuid4(), uuid4(), uuid4(), uuid4()
    source_id = uuid4()
    payload = NotificationEmit(
        dedupe_key=f"highlight:{definition_id}:1:{'a' * 64}:{rule_id}:{version_id}",
        kind="dashboard_highlight",
        params={"definition_id": str(definition_id), "definition_revision": 1, "severity": "info"},
    )
    order: list[str] = []
    seen: dict[str, object] = {}

    async def admit(*_a: object, **_k: object) -> AccessFence:
        order.append("admit")
        return FENCE

    async def locator(*_a: object, **kw: object) -> tuple[object, object]:
        order.append("locator")
        assert kw["scope"] is OWNER
        return document_id, source_id

    async def lock(_session: object, _source: object, **kw: object) -> object:
        order.append("lock")
        seen["fence"] = kw["expected_access_fence"]
        return SimpleNamespace(id=source_id, generation=2)

    async def ready(*_a: object, **kw: object) -> object:
        order.append("ready")
        assert kw["scope"] is OWNER
        return SimpleNamespace(document_id=document_id, source_id=source_id, source_generation=2)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: uuid4()))
    with (
        patch("modules.notifications.public.workspaces.read_access_fence", admit),
        patch("modules.knowledge.documents.public.review_version_locator", locator),
        patch("modules.sources.public.lock_retained_evidence_source", lock),
        patch("modules.knowledge.documents.public.get_ready_version_ref", ready),
    ):
        ok = await public.emit(
            session, payload, evidence=NotificationEvidence(document_id, version_id),
            scope=OWNER, multi_workspace_enabled=FLAG,
        )
    assert ok is True
    assert order == ["admit", "locator", "lock", "ready"]
    assert seen["fence"] == FENCE


def test_frozen_signatures_match_n_callers() -> None:
    emit = inspect.signature(public.emit)
    assert list(emit.parameters) == ["session", "payload", "evidence", "scope", "multi_workspace_enabled"]
    assert emit.parameters["evidence"].kind is inspect.Parameter.KEYWORD_ONLY
    assert emit.parameters["scope"].default is inspect.Parameter.empty
    listing = inspect.signature(public.list_notifications)
    assert list(listing.parameters) == ["session", "unread_only", "limit", "scope", "multi_workspace_enabled"]
    assert listing.parameters["unread_only"].default is False and listing.parameters["limit"].default == 50
    read = inspect.signature(public.set_read)
    assert list(read.parameters) == ["session", "notification_id", "read", "scope", "multi_workspace_enabled"]
