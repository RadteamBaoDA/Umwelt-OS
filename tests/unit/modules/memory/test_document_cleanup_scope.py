"""Memory copied-evidence cleanup: owner admission, workspace-bound SQL and held-fence checks."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.memory import public
from modules.memory.public import SourceCopiedEvidenceScope
from modules.sources.schemas import SourceFence
from tests.unit.modules._cleanup_scope import (
    CTX,
    FENCE,
    MEMBER_CTX,
    OWNER,
    WS,
    Recorder,
    admitted,
    evidence,
)


async def test_document_page_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await public.purge_document_copied_evidence_page(session, evidence(), cursor=None, **MEMBER_CTX)  # type: ignore[arg-type]
    assert caught.value.status_code == 403 and session.sql == []


async def test_document_page_rejects_foreign_workspace_evidence() -> None:
    session = Recorder()
    for bad in (evidence(workspace_id=uuid4()), evidence(actor_user_id=99)):
        with admitted(), pytest.raises(HTTPException) as caught:
            await public.purge_document_copied_evidence_page(session, bad, cursor=None, **CTX)  # type: ignore[arg-type]
        assert caught.value.status_code == 409
    assert session.sql == []


async def test_document_page_sql_is_workspace_and_actor_bound() -> None:
    session = Recorder()
    with admitted():
        progress = await public.purge_document_copied_evidence_page(session, evidence(), cursor=None, **CTX)  # type: ignore[arg-type]
    assert progress.complete
    selects = session.selects()
    assert selects and all("workspace_id = " in text and "actor_user_id = " in text for text in selects)
    session.assert_selects_bound(WS, 7)


async def test_lock_export_privacy_requires_current_fence() -> None:
    session = Recorder()
    stale = AccessFence(workspace_id=WS, user_id=7, membership_revision=9, configuration_revision=1)
    with admitted(), pytest.raises(HTTPException) as caught:
        await public.lock_export_privacy_in_uow(
            session, scope=OWNER, multi_workspace_enabled=False, access_fence=stale,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 409 and session.sql == []
    with admitted():
        await public.lock_export_privacy_in_uow(
            session, scope=OWNER, multi_workspace_enabled=False, access_fence=FENCE,  # type: ignore[arg-type]
        )
    assert "pg_advisory_xact_lock" in session.sql[0]


def _source_args(source_id, generation, fence=None):
    job = InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=2,
                           source_id=source_id, source_generation=generation)
    return job, SourceFence(id=source_id, workspace_id=WS, status="purging", generation=generation, local_only=False)


async def test_source_page_binds_fence_receipt_and_workspace() -> None:
    session = Recorder()
    source_id = uuid4()
    job, source_fence = _source_args(source_id, 3)
    evid = SourceCopiedEvidenceScope(operation_id=uuid4(), source_id=source_id, generation=3)
    kw = {"scope": job, "multi_workspace_enabled": False, "source_fence": source_fence}
    with admitted():
        stale = AccessFence(workspace_id=WS, user_id=7, membership_revision=9, configuration_revision=1)
        with pytest.raises(HTTPException) as caught:
            await public.purge_source_copied_evidence_page_in_uow(session, evid, access_fence=stale, **kw)  # type: ignore[arg-type]
        assert caught.value.status_code == 409
        other = SourceCopiedEvidenceScope(operation_id=evid.operation_id, source_id=source_id, generation=4)
        with pytest.raises(HTTPException):
            await public.purge_source_copied_evidence_page_in_uow(session, other, access_fence=FENCE, **kw)  # type: ignore[arg-type]
        assert session.sql == []
        progress = await public.purge_source_copied_evidence_page_in_uow(session, evid, access_fence=FENCE, **kw)  # type: ignore[arg-type]
    assert progress.complete
    selects = session.selects()
    assert selects and all("workspace_id = " in text and "actor_user_id = " in text for text in selects)
    session.assert_selects_bound(WS, 7)
