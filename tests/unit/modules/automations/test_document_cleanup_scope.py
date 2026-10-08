"""Automations document scrubs: owner admission, workspace/actor-bound SQL, scoped provenance calls."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.automations import execution
from tests.unit.modules._cleanup_scope import CTX, MEMBER_CTX, Recorder, admitted

KW = {"operation_id": uuid4(), "document_id": uuid4(), "source_id": uuid4(), "version_ids": ()}


@pytest.mark.parametrize("hook", [execution.scrub_document_triggers, execution.scrub_document_runs])
async def test_member_denied_before_sql(hook) -> None:  # type: ignore[no-untyped-def]
    session = Recorder()
    with admitted(), pytest.raises(HTTPException) as caught:
        await hook(session, **KW, **MEMBER_CTX)
    assert caught.value.status_code == 403 and session.sql == []


@pytest.mark.parametrize(
    ("hook", "table"),
    [(execution.scrub_document_triggers, "automation_triggers"), (execution.scrub_document_runs, "automation_runs")],
)
async def test_page_sql_is_workspace_and_owner_bound(hook, table) -> None:  # type: ignore[no-untyped-def]
    session = Recorder()
    with admitted():
        progress = await hook(session, **KW, **CTX)
    assert progress.complete
    where = session.selects()[0].split("WHERE", 1)[1]
    assert f"{table}.workspace_id = " in where and f"{table}.owner_id = " in where


async def test_ingestion_resolvers_receive_scope_and_flag() -> None:
    resolver = AsyncMock(return_value=None)
    with patch("modules.ingestion.public.resolve_ready_event_provenance", resolver):
        await execution._legacy_event_matches_cleanup(
            Recorder(), uuid4(), document_id=uuid4(), source_id=uuid4(), version_ids=(), **CTX,  # type: ignore[arg-type]
        )
        await execution._event_belongs_elsewhere(
            Recorder(), str(uuid4()), document_id=uuid4(), **CTX,  # type: ignore[arg-type]
        )
    assert resolver.await_count == 2
    for call in resolver.await_args_list:
        assert call.kwargs["scope"] is CTX["scope"] and call.kwargs["multi_workspace_enabled"] is False
