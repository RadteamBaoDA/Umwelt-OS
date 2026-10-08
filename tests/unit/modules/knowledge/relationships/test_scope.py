"""Workspace-scope contracts for Relationships: member denial, scoped predicates, seed identity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.knowledge.entities.models import Entity
from modules.knowledge.entities.seed import ensure_demo_entities
from modules.knowledge.relationships import public
from modules.knowledge.relationships.models import Relationship
from modules.knowledge.relationships.schemas import RelationshipCreate
from modules.knowledge.relationships.seed import ensure_demo_relationships

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
    return patch("modules.knowledge.relationships.public.workspaces.read_access_fence", AsyncMock(return_value=MagicMock()))


@pytest.mark.asyncio
async def test_member_without_share_sees_nothing() -> None:
    """Every public entry point rejects a member before reading or locking any row."""
    session = AsyncMock()
    payload = RelationshipCreate(source_entity_id=uuid4(), target_entity_id=uuid4(), type="KNOWS", origin="owner")
    calls = [
        public.list_relationships(session, 10, None, **MKW),
        public.get_neighbors(session, uuid4(), 10, None, **MKW),
        public.list_relationship_evidence(session, uuid4(), 10, None, **MKW),
        public.create_relationship(session, payload, **MKW),
        public.remove_relationship(session, uuid4(), **MKW),
        public.lock_relationship_ids(session, [uuid4()], **MKW),
        public.export_page(session, owner_id=8, record_kind="relationships", **MKW),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 403
    session.scalar.assert_not_called()
    session.scalars.assert_not_called()
    session.execute.assert_not_called()


def test_evidence_scope_goes_through_the_workspace_owned_relationship() -> None:
    """Support rows have no workspace column; the predicate must reach the scoped parent."""
    text, params = _sql(public._evidence_scope(OWNER))
    assert "relationships_1.workspace_id" in text
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_list_filters_workspace_before_limit() -> None:
    """Current-state pages apply the workspace predicate before ordering and LIMIT."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    with _admitted():
        await public.list_relationships(session, 5, None, **KW)
    text, params = _sql(session.scalars.call_args.args[0])
    assert text.index("relationships.workspace_id") < text.index("ORDER BY") < text.index("LIMIT")
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_foreign_relationship_evidence_is_absent() -> None:
    """Evidence of a relationship from another workspace is reported as absent, via a scoped lookup."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    with _admitted():
        assert await public.list_relationship_evidence(session, uuid4(), 10, None, **KW) == (None, None)
    text, params = _sql(session.scalar.call_args.args[0])
    assert "relationships.workspace_id" in text
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_remove_foreign_relationship_returns_false_under_lock() -> None:
    """Deleting a foreign id locks the workspace fence first and then finds nothing."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    with patch("modules.knowledge.relationships.public.workspaces.lock_access_fence",
               AsyncMock(return_value=MagicMock())) as locked:
        assert await public.remove_relationship(session, uuid4(), **KW) is False
    locked.assert_awaited_once()
    _text, params = _sql(session.scalar.call_args.args[0])
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_export_rejects_other_actor() -> None:
    """An export for an owner id that is not the scope's actor is refused."""
    with _admitted(), pytest.raises(PermissionError):
        await public.export_page(AsyncMock(), owner_id=999, record_kind="relationships", **KW)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [OWNER, InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=7, membership_revision=1)])
async def test_demo_seed_is_workspace_local_and_links_seeded_entities(scope: WorkspaceContext | InternalJobScope) -> None:
    """The seeded relationship carries the workspace and joins exactly the entities the entity seed creates."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    with patch("modules.knowledge.entities.seed.workspaces.read_access_fence", AsyncMock()), \
            patch("modules.knowledge.relationships.seed.workspaces.read_access_fence", AsyncMock()):
        await ensure_demo_entities(session, scope=scope, multi_workspace_enabled=False)
        await ensure_demo_relationships(session, scope=scope, multi_workspace_enabled=False)
    added = [call.args[0] for call in session.add.call_args_list]
    entity_ids = {item.id for item in added if isinstance(item, Entity)}
    relationship = next(item for item in added if isinstance(item, Relationship))
    assert relationship.workspace_id == OWNER.workspace_id
    assert {relationship.source_entity_id, relationship.target_entity_id} == entity_ids
