"""Member document reads: grant subquery before LIMIT, owner-only 403s, 404s, cursors, revoke hook."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents import public, routes
from modules.knowledge.documents.models import Document
from tests.unit.modules.knowledge.documents._scope import FENCE, SCOPE, WORKSPACE_ID

MEMBER = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=3)
MFENCE = AccessFence(workspace_id=WORKSPACE_ID, user_id=2, membership_revision=3, configuration_revision=1)
GRANTS = select(Document.id).where(Document.title == "grant-marker")
REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
    settings=SimpleNamespace(multi_workspace_enabled=False))))


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class _Session:
    def __init__(self) -> None:
        self.statements: list = []
        self.scalars = AsyncMock(side_effect=self._scalars)

    async def _scalars(self, statement):
        self.statements.append(statement)
        return MagicMock(all=list)

    async def scalar(self, statement):
        self.statements.append(statement)


def _grant_patches(fence=MFENCE):
    return (patch.object(public.workspaces, "granted_resource_ids", MagicMock(return_value=GRANTS)),
            patch.object(public, "read_access_fence", AsyncMock(return_value=fence)))


@pytest.mark.asyncio
async def test_member_list_has_grant_subquery_before_limit() -> None:
    session = _Session()
    grants, fence = _grant_patches()
    with grants, fence:
        await public.list_documents(session, 10, None, None, scope=MEMBER, multi_workspace_enabled=True)
    sql = _sql(session.statements[0])
    assert "documents.id IN (SELECT documents.id" in sql
    assert sql.index("documents.id IN (SELECT") < sql.index("LIMIT")


@pytest.mark.asyncio
async def test_owner_list_has_no_grant_subquery() -> None:
    session = _Session()
    granted = MagicMock(return_value=GRANTS)
    with patch.object(public.workspaces, "granted_resource_ids", granted), \
            patch.object(public, "read_access_fence", AsyncMock(return_value=FENCE)):
        await public.list_documents(session, 10, None, None, scope=SCOPE, multi_workspace_enabled=True)
    granted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda s: public.create_document(s, SimpleNamespace(), scope=MEMBER, multi_workspace_enabled=True),
    lambda s: public.update_document(s, uuid4(), SimpleNamespace(), scope=MEMBER, multi_workspace_enabled=True),
    lambda s: public.delete_document(s, uuid4(), scope=MEMBER, multi_workspace_enabled=True),
    lambda s: public.append_content(s, uuid4(), 1, "x", scope=MEMBER, multi_workspace_enabled=True),
    lambda s: public.get_document_cleanup_operation(s, uuid4(), scope=MEMBER, multi_workspace_enabled=True),
])
async def test_member_mutations_stay_forbidden(call) -> None:
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await call(session)
    assert exc.value.status_code == 403
    assert session.statements == []


@pytest.mark.asyncio
async def test_guessed_id_is_404_via_route() -> None:
    grants, fence = _grant_patches()
    with grants, fence, pytest.raises(HTTPException) as exc:
        await routes.get_document(uuid4(), _Session(), REQUEST, MEMBER)
    assert exc.value.status_code == 404


def test_cursor_from_another_actor_is_rejected() -> None:
    cursor = public._encode_document_owner_cursor("pos", kind="documents", fence=FENCE, resource_id=None)
    with pytest.raises(HTTPException) as exc:
        public._decode_document_owner_cursor(cursor, kind="documents", fence=MFENCE, resource_id=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_member_route_arms_gate_with_grants_and_projects_member_dto() -> None:
    now = datetime.now(UTC)
    doc = SimpleNamespace(
        id=uuid4(), title="t", content_type="x", mime_type="m", canonical_url=None, author=None,
        current_version=2, content_hash="h", extraction_status="ready", published_at=None,
        observed_at=None, language=None, raw_uri="secret/path", created_at=now, updated_at=now,
        source_id=uuid4(), metadata_json={})
    grant = SimpleNamespace(resource_id=doc.id)
    gate = MagicMock()
    with patch.object(routes.public, "get_document", AsyncMock(return_value=doc)), \
            patch.object(routes, "read_resource_grants", AsyncMock(return_value=(grant,))), \
            patch.object(routes, "read_access_fence", AsyncMock(return_value=MFENCE)), \
            patch.object(routes, "authenticated_session_ref", MagicMock(return_value="ref")), \
            patch.object(routes, "require_publication_gate", gate):
        result = await routes.get_document(doc.id, _Session(), REQUEST, MEMBER)
    dumped = result.model_dump()
    assert dumped["has_raw"] is True and "raw_uri" not in dumped and "source_id" not in dumped
    fence = gate.call_args.args[1]
    assert fence.grants == (grant,) and fence.scope == MEMBER


@pytest.mark.asyncio
async def test_route_fails_closed_when_grant_vanished() -> None:
    with patch.object(routes, "read_resource_grants", AsyncMock(return_value=())), \
            pytest.raises(HTTPException) as exc:
        await routes._gate_member_read(REQUEST, _Session(), MEMBER, (uuid4(),))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_owner_route_skips_gate() -> None:
    gate = MagicMock()
    with patch.object(routes, "require_publication_gate", gate):
        await routes._gate_member_read(REQUEST, _Session(), SCOPE, (uuid4(),))
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_owner_only_routes_still_deny_members() -> None:
    with pytest.raises(HTTPException) as exc:
        await routes.get_deletion_operation(uuid4(), _Session(), REQUEST, MEMBER)
    assert exc.value.status_code == 403
