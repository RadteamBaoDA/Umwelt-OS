"""Workspace-scope contracts for frozen Documents readers (compiled SQL, denial, cursors; no DB)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents import public
from modules.knowledge.observations import public as observations
from tests.unit.modules.knowledge.documents.conftest import FENCE, SCOPE_KW, WORKSPACE_ID


class _Session:
    """Record every statement; results are empty."""

    def __init__(self) -> None:
        self.statements: list = []
        self.scalars = AsyncMock(return_value=SimpleNamespace(all=list))

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=list, one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=list))

    async def scalar(self, statement):
        self.statements.append(statement)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_review_version_fences_reader_carries_workspace_predicate() -> None:
    session = _Session()
    await public.review_version_fences(session, [uuid4()], **SCOPE_KW)
    assert "workspace_id" in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_member_without_share_reads_nothing() -> None:
    member = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await public.review_version_fences(session, [uuid4()], scope=member, multi_workspace_enabled=True)
    assert exc.value.status_code == 403
    assert session.statements == []


@pytest.mark.asyncio
async def test_lock_chat_evidence_chunks_never_locks_foreign_identity() -> None:
    session = _Session()
    lock_sources = AsyncMock()
    with patch.object(public.sources, "lock_source_set", lock_sources):
        result = await public.lock_chat_evidence_chunks(
            session, [(uuid4(), uuid4())], **SCOPE_KW,
        )
    assert result == []
    assert "workspace_id" in _sql(session.statements[0])
    lock_sources.assert_not_awaited()
    session.scalars.assert_not_awaited()  # no Document/version/chunk row lock without scoped identities


def test_owner_cursor_rejects_revision_mismatch_and_foreign_context() -> None:
    cursor = public._encode_document_owner_cursor("pos", kind="k", fence=FENCE, resource_id=None)
    assert public._decode_document_owner_cursor(cursor, kind="k", fence=FENCE, resource_id=None) == "pos"
    stale = AccessFence(WORKSPACE_ID, 1, 2, 1)
    with pytest.raises(HTTPException) as exc:
        public._decode_document_owner_cursor(cursor, kind="k", fence=stale, resource_id=None)
    assert exc.value.status_code == 409
    other = AccessFence(uuid4(), 1, 1, 1)
    with pytest.raises(HTTPException) as exc:
        public._decode_document_owner_cursor(cursor, kind="k", fence=other, resource_id=None)
    assert exc.value.status_code == 422


def test_v3_export_cursors_reject_revision_mismatch() -> None:
    from datetime import UTC, datetime

    now, row = datetime.now(UTC), uuid4()
    doc = public._encode_document_export_cursor(1, WORKSPACE_ID, "documents", now, now, row, FENCE)
    obs = observations._observation_export_cursor(1, WORKSPACE_ID, now, now, row, FENCE)
    assert public._decode_document_export_cursor(doc, 1, WORKSPACE_ID, "documents", FENCE)[2] == row
    assert observations._decode_observation_export_cursor(obs, 1, WORKSPACE_ID, FENCE)[2] == row
    stale = AccessFence(WORKSPACE_ID, 1, 1, 2)
    with pytest.raises(ValueError):
        public._decode_document_export_cursor(doc, 1, WORKSPACE_ID, "documents", stale)
    with pytest.raises(Exception):  # noqa: B017 - observation decoder raises its own HTTP/Value error
        observations._decode_observation_export_cursor(obs, 1, WORKSPACE_ID, stale)

