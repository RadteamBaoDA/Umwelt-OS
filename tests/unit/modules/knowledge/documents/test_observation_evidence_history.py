"""Fences of ``current_observation_evidence_versions`` with and without ``include_historical``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents.public import (
    ObservationEvidenceCandidate,
    current_observation_evidence_versions,
)
from tests.unit.modules.knowledge.documents._scope import SCOPE_KW

DISC = "scope-digest"


def _case(**overrides):
    """Build one candidate, its matching current scope and the single provenance row it matches."""
    source_id, doc_id, version_id, obs_id = uuid4(), uuid4(), uuid4(), uuid4()
    candidate = ObservationEvidenceCandidate(
        observation_id=obs_id, source_id=source_id, source_generation=3, provider="prov",
        provider_scope_discriminator=DISC, external_id="ext", document_id=doc_id, document_version_id=version_id,
    )
    scopes = {source_id: SimpleNamespace(provider_id="prov", source_generation=3, discriminator=DISC)}
    # (doc, version, source, source_generation, version_number, current_version, provider_id, accepted_gen, provenance)
    row = [doc_id, version_id, source_id, 3, 1, 2, "ext", 3, {"provider_scope_discriminator": DISC}]
    for index, value in overrides.pop("row", {}).items():
        row[index] = value
    return candidate, scopes, [tuple(row)]


async def _run(candidate, scopes, rows, historical, scope=SCOPE_KW["scope"]):
    """Execute against a fake session that returns ``rows``; return (result, session)."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    result = await current_observation_evidence_versions(
        session, [candidate], scopes, scope=scope, multi_workspace_enabled=False, include_historical=historical,
    )
    return result, session


async def test_superseded_version_rejected_by_default_accepted_when_historical() -> None:
    """version_number 1 under current_version 2 is only evidence in history mode."""
    candidate, scopes, rows = _case()
    default, _ = await _run(candidate, scopes, rows, False)
    historical, _ = await _run(candidate, scopes, rows, True)
    assert default == {}
    assert historical == {candidate.observation_id: 1}


FENCES = {
    "source": {"row": {2: uuid4()}},
    "source_generation": {"row": {3: 9}},
    "provider_id": {"row": {6: "other"}},
    "accepted_generation": {"row": {7: 9}},
    "discriminator": {"row": {8: {"provider_scope_discriminator": "other"}}},
    "provenance_not_dict": {"row": {8: None}},
    "provenance_row_missing": {"missing": True},
    "duplicate_provenance": {"duplicate": True},
}


@pytest.mark.parametrize("name", sorted(FENCES))
async def test_historical_mode_keeps_every_other_fence(name: str) -> None:
    """A superseded version is still rejected in history mode when any other fence fails."""
    spec = FENCES[name]
    candidate, scopes, rows = _case(row=dict(spec.get("row", {})))
    if spec.get("missing"):
        rows = []
    if spec.get("duplicate"):
        rows = rows * 2
    result, _ = await _run(candidate, scopes, rows, True)
    assert result == {}


@pytest.mark.parametrize("scope_key", ["provider", "source_generation", "discriminator", "absent"])
async def test_historical_mode_requires_current_provider_scope(scope_key: str) -> None:
    """History mode never accepts evidence whose live provider scope no longer matches."""
    candidate, scopes, rows = _case()
    live = scopes[candidate.source_id]
    if scope_key == "absent":
        scopes = {}
    else:
        setattr(live, {"provider": "provider_id", "source_generation": "source_generation",
                       "discriminator": "discriminator"}[scope_key], "changed" if scope_key != "source_generation" else 9)
    result, _ = await _run(candidate, scopes, rows, True)
    assert result == {}


async def test_historical_mode_query_keeps_workspace_and_purge_fences() -> None:
    """The SQL fence (workspace root + purge-excluding eligible sources) is identical in history mode."""
    candidate, scopes, rows = _case()
    for historical in (False, True):
        _, session = await _run(candidate, scopes, rows, historical)
        sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "documents.workspace_id" in sql and "source_purge_operations" in sql
        assert "document_versions.id IN" in sql


async def test_historical_mode_still_requires_owner_scope() -> None:
    """A non-owner workspace scope is refused before any query, history mode or not."""
    from fastapi import HTTPException

    from core.workspaces.schemas import WorkspaceContext

    member = WorkspaceContext(user_id=2, workspace_id=SCOPE_KW["scope"].workspace_id, role="member", membership_revision=1)
    candidate, scopes, rows = _case()
    with pytest.raises(HTTPException):
        await _run(candidate, scopes, rows, True, scope=member)
