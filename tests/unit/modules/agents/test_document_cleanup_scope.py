"""Agents copied-evidence cleanup: owner admission, workspace-bound candidate SQL, evidence binding."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.agents import public as agents
from tests.unit.modules._cleanup_scope import CTX, MEMBER_CTX, Recorder, admitted, evidence


def _preflight() -> agents.AgentCleanupLeasePreflight:
    return agents.AgentCleanupLeasePreflight(uuid4(), "x", None, None, "none", False, False, False)


async def test_preflight_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await agents.preflight_document_copied_evidence_lease(session, evidence(), **MEMBER_CTX)  # type: ignore[arg-type]
    assert caught.value.status_code == 403 and session.sql == []


async def test_preflight_and_purge_reject_foreign_evidence_before_sql() -> None:
    session = Recorder()
    for bad in (evidence(workspace_id=uuid4()), evidence(actor_user_id=99)):
        with admitted(), pytest.raises(HTTPException) as caught:
            await agents.preflight_document_copied_evidence_lease(session, bad, **CTX)  # type: ignore[arg-type]
        assert caught.value.status_code == 409
        with admitted(), pytest.raises(HTTPException):
            await agents.purge_document_copied_evidence_page(
                session, bad, preflight=_preflight(), **CTX,  # type: ignore[arg-type]
            )
    assert session.sql == []


async def test_preflight_candidate_sql_is_workspace_and_owner_bound() -> None:
    session = Recorder()
    with admitted():
        result = await agents.preflight_document_copied_evidence_lease(session, evidence(), **CTX)  # type: ignore[arg-type]
    assert result.candidate_run_id is None
    where = session.selects()[0].split("WHERE", 1)[1]
    assert "agent_runs.workspace_id = " in where and "agent_runs.owner_id = " in where
    assert "agent_evidence_cleanups.workspace_id = " in where


async def test_purge_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await agents.purge_document_copied_evidence_page(
            session, evidence(), preflight=_preflight(), **MEMBER_CTX,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 403 and session.sql == []
