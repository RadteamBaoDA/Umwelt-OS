"""ReadyVersionRef producers carry the Document raw_uri/mime_type fence inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.knowledge.documents import public
from tests.unit.modules.knowledge.documents._scope import SCOPE_KW

ROW = (uuid4(), datetime(2026, 1, 1, tzinfo=UTC), uuid4(), 3, uuid4(), 2, False, "raw://x", "text/markdown")


def _session(result: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_get_ready_version_ref_carries_fence() -> None:
    result = MagicMock()
    result.one_or_none.return_value = ROW
    session = _session(result)
    with patch.object(public, "_admit_document_scope", AsyncMock()):
        ref = await public.get_ready_version_ref(session, ROW[4], **SCOPE_KW)
    assert (ref.raw_uri, ref.mime_type) == ("raw://x", "text/markdown")
    sql = str(session.execute.call_args.args[0])
    assert "documents.raw_uri" in sql and "documents.mime_type" in sql


@pytest.mark.asyncio
async def test_list_ready_version_refs_carries_fence() -> None:
    result = MagicMock()
    result.all.return_value = [ROW]
    session = _session(result)
    with patch.object(public, "_admit_document_scope", AsyncMock()):
        refs, cursor = await public.list_ready_version_refs(session, **SCOPE_KW)
    assert cursor is None and (refs[0].raw_uri, refs[0].mime_type) == ("raw://x", "text/markdown")
    sql = str(session.execute.call_args.args[0])
    assert "documents.raw_uri" in sql and "documents.mime_type" in sql
