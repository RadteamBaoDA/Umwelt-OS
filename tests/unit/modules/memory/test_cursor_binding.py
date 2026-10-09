"""Memory/Agents page cursors are bound to workspace, actor and list filters (P3-5)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.pagination import decode_cursor, encode_cursor
from core.workspaces.schemas import WorkspaceContext
from modules.memory.public import bind_page_cursor, page_cursor_binding, unbind_page_cursor


def _scope(user_id: int = 7, workspace_id=None) -> WorkspaceContext:
    return WorkspaceContext(
        user_id=user_id, workspace_id=workspace_id or uuid4(), role="owner", membership_revision=1,
    )


def test_cursor_round_trips_for_same_binding() -> None:
    scope = _scope()
    binding = page_cursor_binding(scope, kind="memories", status="active")
    position = encode_cursor(datetime.now(UTC), uuid4())
    cursor = bind_page_cursor(position, binding)
    assert unbind_page_cursor(cursor, binding) == position
    decode_cursor(position)


def test_binding_differs_by_workspace_actor_and_filters() -> None:
    scope = _scope()
    base = page_cursor_binding(scope, kind="memories", status="active")
    assert base == page_cursor_binding(scope, kind="memories", status="active")
    assert base != page_cursor_binding(_scope(workspace_id=uuid4()), kind="memories", status="active")
    assert base != page_cursor_binding(_scope(user_id=8, workspace_id=scope.workspace_id), kind="memories", status="active")
    assert base != page_cursor_binding(scope, kind="memories", status="forgotten")
    assert base != page_cursor_binding(scope, kind="candidates", status="active")


@pytest.mark.parametrize("bad", ["x", "", "e30", "a" * 3000])
def test_foreign_or_malformed_cursor_is_422(bad: str) -> None:
    binding = page_cursor_binding(_scope(), kind="memories")
    with pytest.raises(HTTPException) as caught:
        unbind_page_cursor(bad, binding)
    assert caught.value.status_code == 422


def test_cursor_from_another_workspace_is_rejected() -> None:
    mine, theirs = _scope(), _scope()
    cursor = bind_page_cursor(
        encode_cursor(datetime.now(UTC), uuid4()), page_cursor_binding(theirs, kind="agent_runs"),
    )
    with pytest.raises(HTTPException) as caught:
        unbind_page_cursor(cursor, page_cursor_binding(mine, kind="agent_runs"))
    assert caught.value.status_code == 422


def test_unbound_legacy_cursor_is_rejected() -> None:
    legacy = encode_cursor(datetime.now(UTC), uuid4())
    with pytest.raises(HTTPException):
        unbind_page_cursor(legacy, page_cursor_binding(_scope(), kind="memories"))
