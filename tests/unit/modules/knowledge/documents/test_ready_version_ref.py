"""ReadyVersionRef producers carry the Document raw_uri/mime_type fence inputs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.pagination import encode_cursor
from modules.knowledge.documents import public
from modules.knowledge.documents._documents import (
    _decode_document_owner_cursor,
    _encode_document_owner_cursor,
)
from tests.unit.modules._documents_patch import patch_documents
from tests.unit.modules.knowledge.documents._scope import FENCE, SCOPE_KW

ROW = (
    uuid4(),
    datetime(2026, 1, 1, tzinfo=UTC),
    uuid4(),
    3,
    uuid4(),
    2,
    False,
    "raw://x",
    "text/markdown",
)


def _session(result: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_get_ready_version_ref_carries_fence() -> None:
    result = MagicMock()
    result.one_or_none.return_value = ROW
    session = _session(result)
    with patch_documents("_admit_document_scope", AsyncMock()):
        ref = await public.get_ready_version_ref(session, ROW[4], **SCOPE_KW)
    assert (ref.raw_uri, ref.mime_type) == ("raw://x", "text/markdown")
    sql = str(session.execute.call_args.args[0])
    assert "documents.raw_uri" in sql and "documents.mime_type" in sql


@pytest.mark.asyncio
async def test_list_ready_version_refs_carries_fence() -> None:
    result = MagicMock()
    result.all.return_value = [ROW]
    session = _session(result)
    with patch_documents("_admit_document_scope", AsyncMock()):
        refs, cursor = await public.list_ready_version_refs(session, **SCOPE_KW)
    assert cursor is None and (refs[0].raw_uri, refs[0].mime_type) == ("raw://x", "text/markdown")
    sql = str(session.execute.call_args.args[0])
    assert "documents.raw_uri" in sql and "documents.mime_type" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["fence-stale", "malformed"])
async def test_list_ready_version_refs_restarts_on_unusable_cursor(stored: str) -> None:
    """A Redis cursor from an older configuration revision (or garbage) restarts the scan.

    Regression: recovery workers persisted a cursor stamped with configuration_revision=1; after a
    settings change bumped the workspace revision, every cron tick raised 409 and never advanced.
    """
    current = replace(FENCE, configuration_revision=4)
    cursor = (
        _encode_document_owner_cursor(
            encode_cursor(ROW[1], ROW[0]), kind="ready_versions", fence=FENCE, resource_id=None
        )
        if stored == "fence-stale"
        else "not-a-cursor"
    )
    result = MagicMock()
    result.all.return_value = [ROW, ROW]
    session = _session(result)
    with patch_documents("_admit_document_scope", AsyncMock(return_value=current)):
        refs, next_cursor = await public.list_ready_version_refs(
            session, limit=1, cursor=cursor, **SCOPE_KW
        )
    assert len(refs) == 1
    sql = str(session.execute.call_args.args[0])
    assert "(documents.created_at, documents.id) <" not in sql
    # The replacement cursor carries the current fence, so the next tick decodes cleanly.
    assert next_cursor is not None
    _decode_document_owner_cursor(
        next_cursor, kind="ready_versions", fence=current, resource_id=None
    )
