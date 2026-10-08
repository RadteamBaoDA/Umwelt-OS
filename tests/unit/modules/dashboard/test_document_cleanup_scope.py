"""Dashboard brief cleanup: owner admission and workspace/owner-bound brief SQL."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.dashboard import briefs
from tests.unit.modules._cleanup_scope import CTX, MEMBER_CTX, Recorder, admitted


async def test_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted():
        for call in (
            briefs.clean_document_brief_evidence(session, uuid4(), **MEMBER_CTX),  # type: ignore[arg-type]
            briefs.legacy_brief_coverage(session, **MEMBER_CTX),  # type: ignore[arg-type]
        ):
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == 403
    assert session.sql == []


async def test_clean_page_sql_is_workspace_bound_through_the_brief() -> None:
    session = Recorder()
    with admitted():
        progress = await briefs.clean_document_brief_evidence(session, uuid4(), **CTX)  # type: ignore[arg-type]
    assert progress.processed_count == 0
    where = session.selects()[0].split("WHERE", 1)[1]
    assert "daily_briefs.workspace_id = " in where and "daily_briefs.owner_id = " in where


async def test_legacy_coverage_unbounded_still_workspace_bound() -> None:
    session = Recorder()
    with admitted():
        page = await briefs.legacy_brief_coverage(session, not_before=None, **CTX)  # type: ignore[arg-type]
    assert page.candidate_ids == []
    where = session.selects()[0].split("WHERE", 1)[1]
    assert "daily_briefs.workspace_id = " in where and "generated_at >=" not in where
