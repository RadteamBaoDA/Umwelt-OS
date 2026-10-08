"""Unit tests for the Memory write fence that rejects evidence from purged or ineligible Sources."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext
from modules.memory import public

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
CTX = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture
def sources():
    from modules.sources import public as sources_public

    source = SimpleNamespace(generation=3)
    with patch.object(sources_public, "lock_source", AsyncMock(return_value=source)) as lock, \
            patch.object(sources_public, "filter_export_eligible_sources", AsyncMock(return_value=[1])) as eligible, \
            patch.object(sources_public, "source_data_purge_exists", AsyncMock(return_value=False)) as purged:
        yield SimpleNamespace(lock=lock, eligible=eligible, purged=purged)


async def test_source_only_provenance_passes_when_eligible_and_never_purged(sources) -> None:
    source_id = uuid4()
    await public._lock_live_provenance_evidence(
        None, {"source_id": str(source_id)}, require_copy_evidence=False, **CTX,
    )
    sources.lock.assert_awaited_once()
    sources.purged.assert_awaited_once_with(
        None, source_id, scope=SCOPE, multi_workspace_enabled=False,
    )
    assert sources.lock.await_args.kwargs["scope"] == SCOPE
    # The eligibility fence is built from the admitted workspace, never a caller-supplied one.
    fence = sources.eligible.await_args.args[1][0]
    assert fence.workspace_id == SCOPE.workspace_id


async def test_prior_source_data_purge_rejects_even_when_export_eligible(sources) -> None:
    sources.purged.return_value = True  # a successful purge reopens export eligibility
    with pytest.raises(HTTPException) as caught:
        await public._lock_live_provenance_evidence(
            None, {"source_id": str(uuid4())}, require_copy_evidence=False, **CTX,
        )
    assert caught.value.status_code == 409


async def test_missing_or_ineligible_source_is_rejected(sources) -> None:
    sources.eligible.return_value = []
    with pytest.raises(HTTPException) as caught:
        await public._lock_live_provenance_evidence(
            None, {"source_id": str(uuid4())}, require_copy_evidence=False, **CTX,
        )
    assert caught.value.status_code == 409
    sources.eligible.return_value = [1]
    sources.lock.return_value = None
    with pytest.raises(HTTPException) as caught:
        await public._lock_live_provenance_evidence(
            None, {"source_id": str(uuid4())}, require_copy_evidence=False, **CTX,
        )
    assert caught.value.status_code == 409


@pytest.mark.parametrize("provenance,copy", [
    ("not-a-dict", False),
    ({"source_id": "garbage"}, False),
    ({"source_id": str(uuid4()), "extra": 1}, True),
    ({"source_id": str(uuid4())}, True),  # copied content needs Document or Chat evidence
])
async def test_unverifiable_provenance_is_rejected(sources, provenance, copy) -> None:
    with pytest.raises(HTTPException) as caught:
        await public._lock_live_provenance_evidence(None, provenance, require_copy_evidence=copy, **CTX)
    assert caught.value.status_code == 409


async def test_vanished_evidence_source_hides_document_copy(sources) -> None:
    from modules.knowledge.documents import public as documents_public

    provenance = {"document_id": str(uuid4()), "document_version_id": str(uuid4()), "chunk_id": str(uuid4())}
    with patch.object(documents_public, "read_evidence_refs", AsyncMock(side_effect=ValueError("gone"))),             pytest.raises(HTTPException) as caught:
        await public._lock_live_provenance_evidence(None, provenance, require_copy_evidence=True, **CTX)
    assert caught.value.status_code == 409
