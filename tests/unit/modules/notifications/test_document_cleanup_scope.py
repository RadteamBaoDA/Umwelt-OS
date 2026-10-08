"""Notifications document-evidence scrub: owner admission and workspace/actor-bound SQL."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.notifications import public as notifications
from tests.unit.modules._cleanup_scope import CTX, MEMBER_CTX, Recorder, admitted

KW = {"operation_id": uuid4(), "document_id": uuid4(), "version_ids": ()}


async def test_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await notifications.scrub_document_evidence(session, **KW, **MEMBER_CTX)  # type: ignore[arg-type]
    assert caught.value.status_code == 403 and session.sql == []


async def test_page_sql_is_workspace_and_actor_bound() -> None:
    session = Recorder()
    with admitted():
        progress = await notifications.scrub_document_evidence(session, **KW, **CTX)  # type: ignore[arg-type]
    assert progress.complete
    where = session.selects()[0].split("WHERE", 1)[1]
    assert "notifications.workspace_id = " in where and "notifications.owner_id = " in where
