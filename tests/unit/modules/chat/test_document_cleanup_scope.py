"""Chat copied-evidence cleanup (D2b-owned seam; A2 must keep the signature)."""

import inspect
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.chat import public as chat
from tests.unit.modules._cleanup_scope import CTX, MEMBER_CTX, Recorder, admitted, evidence


def test_signature_is_the_frozen_one() -> None:
    params = inspect.signature(chat.purge_document_copied_evidence_page).parameters
    assert list(params)[:2] == ["session", "evidence"]
    for name in ("scope", "multi_workspace_enabled"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["limit"].default == 100 and params["cursor"].default is None


async def test_member_denied_before_sql() -> None:
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await chat.purge_document_copied_evidence_page(session, evidence(), **MEMBER_CTX)  # type: ignore[arg-type]
    assert caught.value.status_code == 403 and session.sql == []


async def test_foreign_evidence_rejected_before_sql() -> None:
    session = Recorder()
    for bad in (evidence(workspace_id=uuid4()), evidence(actor_user_id=99)):
        with admitted(), pytest.raises(HTTPException) as caught:
            await chat.purge_document_copied_evidence_page(session, bad, **CTX)  # type: ignore[arg-type]
        assert caught.value.status_code == 409
    assert session.sql == []


async def test_page_sql_is_workspace_bound() -> None:
    session = Recorder()
    with admitted():
        progress = await chat.purge_document_copied_evidence_page(session, evidence(), **CTX)  # type: ignore[arg-type]
    assert progress.complete
    selects = session.selects()
    assert any("chat_conversations.workspace_id = " in text for text in selects)
    assert any(".workspace_id = " in text and "response" in text for text in selects)
