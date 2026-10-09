"""Unit tests for brief lineage sharing (mocked persistence)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import GrantRef, WorkspaceContext
from modules.dashboard import brief_sharing as bs

WS = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=1)


def _row(revision: int = 3):
    return SimpleNamespace(id=uuid4(), owner_id=1, workspace_id=WS, revision=revision)


@pytest.fixture
def row(monkeypatch):
    r = _row()
    monkeypatch.setattr(bs, "_row", AsyncMock(return_value=r))
    monkeypatch.setattr(bs, "_is_current", AsyncMock(return_value=True))
    return r


async def _code(call):
    with pytest.raises(HTTPException) as exc:
        await call
    return exc.value.status_code, exc.value.detail


async def test_non_story_fact_is_not_shareable(row, monkeypatch) -> None:
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=None))
    assert await _code(bs.check_brief_shareable(None, row.id, scope=OWNER, member_user_id=2)) == (
        409, "brief_not_shareable")


async def test_legacy_or_stale_capture_is_not_shareable(row, monkeypatch) -> None:
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=frozenset({uuid4()})))
    monkeypatch.setattr(bs, "_is_current", AsyncMock(return_value=False))
    assert (await _code(bs.check_brief_shareable(None, row.id, scope=OWNER, member_user_id=2)))[1] == "brief_not_shareable"


async def test_unshared_dependency_lists_only_owner_visible_ids(row, monkeypatch) -> None:
    shared, hidden, gone = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=frozenset({shared, hidden, gone})))
    monkeypatch.setattr(bs.workspaces, "active_grant_ids", AsyncMock(return_value=frozenset({shared})))
    visible = AsyncMock(return_value=[hidden])
    monkeypatch.setattr(bs.documents, "existing_document_ids", visible)
    status, detail = await _code(bs.check_brief_shareable(None, row.id, scope=OWNER, member_user_id=2))
    assert status == 409 and detail == {"code": "brief_evidence_not_shared", "document_ids": [str(hidden)]}
    assert set(visible.await_args.args[1]) == {hidden, gone}


async def test_fully_granted_brief_is_shareable(row, monkeypatch) -> None:
    doc = uuid4()
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=frozenset({doc})))
    monkeypatch.setattr(bs.workspaces, "active_grant_ids", AsyncMock(return_value=frozenset({doc})))
    await bs.check_brief_shareable(None, row.id, scope=OWNER, member_user_id=2)


async def test_invisible_brief_is_404(monkeypatch) -> None:
    monkeypatch.setattr(bs, "_row", AsyncMock(return_value=None))
    assert (await _code(bs.check_brief_shareable(None, uuid4(), scope=OWNER, member_user_id=2)))[0] == 404


def _grant(kind, rid, rev=3):
    return GrantRef(resource_type=kind, resource_id=rid, share_revision=1, resource_revision=rev)


async def test_revoked_dependency_hides_brief(row, monkeypatch) -> None:
    doc = uuid4()
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=frozenset({doc})))
    grants = AsyncMock(side_effect=[(_grant("brief", row.id),), ()])
    monkeypatch.setattr(bs.workspaces, "read_resource_grants", grants)
    assert await bs._member_grants(None, row, scope=MEMBER, multi_workspace_enabled=False) is None


async def test_stale_brief_grant_revision_hides_brief(row, monkeypatch) -> None:
    monkeypatch.setattr(bs.workspaces, "read_resource_grants",
                        AsyncMock(return_value=(_grant("brief", row.id, rev=2),)))
    assert await bs._member_grants(None, row, scope=MEMBER, multi_workspace_enabled=False) is None


async def test_all_grants_active_returns_publication_grants(row, monkeypatch) -> None:
    doc = uuid4()
    monkeypatch.setattr(bs, "_story_documents", AsyncMock(return_value=frozenset({doc})))
    monkeypatch.setattr(bs.workspaces, "read_resource_grants", AsyncMock(
        side_effect=[(_grant("brief", row.id),), (_grant("document", doc, 1),)]))
    got = await bs._member_grants(None, row, scope=MEMBER, multi_workspace_enabled=False)
    assert got is not None and {g.resource_id for g in got} == {row.id, doc}
