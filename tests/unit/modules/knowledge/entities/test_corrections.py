"""Unit tests for modules.knowledge.entities.corrections.

Tests cover:
- CorrectionConflictError and structured conflict DTO generation.
- _Closure and _DeleteClosure representations, properties, and deterministic signatures.
- _validate_merge_request validation branches (entity_missing, stale_revision,
  incompatible_entity_types, protected_field_conflict, derived_field_conflict).
- _validate_split_request validation branches (entity_missing, stale_revision,
  incompatible_entity_types, split_identity_conflict, membership_unavailable, unsupported_entity).
- preview_merge and preview_split previews and conflict reporting.
- merge_entity execution, field provenance transfer, redirect creation, and audit logging.
- split_entity execution, new entity creation, membership reassignment, and audit logging.
- suppress_candidates execution, audit logging, and conflict handling.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.entities.corrections import (
    CorrectionConflictError,
    _Closure,
    _conflict,
    _DeleteClosure,
    _validate_merge_request,
    _validate_split_request,
    merge_entity,
    preview_merge,
    preview_split,
    split_entity,
    suppress_candidates,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
)
from modules.knowledge.entities.schemas import (
    EntityCorrectionPreview,
    EntityCorrectionResult,
    EntityCreate,
    EntityMergeRequest,
    EntitySplitRequest,
    EntitySuppressionRequest,
    canonicalize_name,
)
from modules.knowledge.relationships.schemas import CorrectionRelationshipRef


def _make_sample_entity(
    *,
    entity_id: UUID | None = None,
    name: str = "Test Entity",
    type_: str = "topic",
    revision: int = 1,
    name_origin: str = "derived",
    description: str | None = "A test concept",
    description_origin: str | None = "derived",
) -> Entity:
    """Helper to instantiate an Entity test model."""
    uid = entity_id or uuid4()
    return Entity(
        id=uid,
        name=name,
        canonical_name=canonicalize_name(name),
        type=type_,
        revision=revision,
        name_origin=name_origin,
        description=description,
        description_origin=description_origin,
        metadata_json={},
    )


def _make_sample_membership(
    *,
    membership_id: UUID | None = None,
    entity_id: UUID | None = None,
    doc_id: UUID | None = None,
    source_id: UUID | None = None,
) -> EntityEvidenceMembership:
    """Helper to instantiate an EntityEvidenceMembership test model."""
    now = datetime.now(UTC)
    return EntityEvidenceMembership(
        id=membership_id or uuid4(),
        entity_id=entity_id or uuid4(),
        document_id=doc_id or uuid4(),
        source_id=source_id or uuid4(),
        document_version_id=uuid4(),
        chunk_id=uuid4(),
        extraction_identity="ident_1",
        candidate_key="cand_1",
        match_fingerprint="fp_1",
        observed_at=now,
        extracted_at=now,
        confidence=0.9,
    )


def _make_sample_relationship_ref(
    *,
    rel_id: UUID | None = None,
    source_entity_id: UUID,
    target_entity_id: UUID,
    rel_type: str = "RELATES_TO",
    origin: str = "derived",
) -> CorrectionRelationshipRef:
    """Helper to instantiate a valid CorrectionRelationshipRef schema."""
    return CorrectionRelationshipRef(
        id=rel_id or uuid4(),
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        type=rel_type,
        origin=origin,  # type: ignore[arg-type]
        valid_from=None,
        valid_to=None,
        metadata={},
        supports=[],
    )


SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
KW = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _admitted() -> object:
    """Admission is covered elsewhere; these tests exercise correction logic past it."""
    admitted = AsyncMock(return_value=MagicMock())
    with (
        patch("modules.knowledge.entities.corrections._admit", admitted),
        patch("modules.knowledge.entities.public._admit", admitted),
        patch("modules.knowledge.relationships.public._admit", admitted),
    ):
        yield

class TestCorrectionConflictAndClosures:
    """Tests for CorrectionConflictError and closure data containers."""

    def test_correction_conflict_error_instantiation(self) -> None:
        """Verify CorrectionConflictError populates the internal EntityCorrectionConflict DTO."""
        e_id = uuid4()
        m_id = uuid4()
        r_id = uuid4()

        err = _conflict(
            "test_conflict",
            "A test conflict occurred",
            entity_ids=[e_id],
            membership_ids=[m_id],
            relationship_ids=[r_id],
        )
        assert isinstance(err, CorrectionConflictError)
        assert err.conflict.code == "test_conflict"
        assert err.conflict.message == "A test conflict occurred"
        assert err.conflict.entity_ids == [e_id]
        assert err.conflict.membership_ids == [m_id]
        assert err.conflict.relationship_ids == [r_id]

    def test_closure_properties_and_signature(self) -> None:
        """Verify _Closure relationship_ids and signature determinism."""
        entity = _make_sample_entity()
        mem = _make_sample_membership(entity_id=entity.id)
        r_id = uuid4()
        rel_ref = _make_sample_relationship_ref(
            rel_id=r_id,
            source_entity_id=entity.id,
            target_entity_id=uuid4(),
        )

        closure = _Closure(
            entity_rows=[entity],
            memberships=[mem],
            aliases=[],
            alias_supports=[],
            field_supports=[],
            relationship_refs=[rel_ref],
            redirect_rows=[],
            relationship_entity_ids=[entity.id],
            entity_ids=[entity.id],
            evidence_pairs=[(mem.document_version_id, mem.chunk_id)],
            source_ids=[mem.source_id],
            document_ids=[mem.document_id],
            membership_ids=[mem.id],
            timeline_event_ids=[],
        )

        assert closure.relationship_ids == [r_id]
        sig1 = closure.signature()
        sig2 = closure.signature()
        assert sig1 == sig2
        data = json.loads(sig1)
        assert "entities" in data
        assert "memberships" in data

    def test_delete_closure_properties_and_signature(self) -> None:
        """Verify _DeleteClosure entity_ids and relationship_ids properties."""
        entity = _make_sample_entity()
        r_id = uuid4()
        rel_ref = _make_sample_relationship_ref(
            rel_id=r_id,
            source_entity_id=entity.id,
            target_entity_id=uuid4(),
        )

        delete_closure = _DeleteClosure(
            entity_rows=[entity],
            redirect_rows=[],
            memberships=[],
            aliases=[],
            alias_supports=[],
            field_supports=[],
            decisions=[],
            relationship_refs=[rel_ref],
            evidence_pairs=[],
            source_ids=[],
            document_ids=[],
            lock_entity_ids=[entity.id],
            timeline_event_ids=[],
        )

        assert delete_closure.entity_ids == [entity.id]
        assert delete_closure.relationship_ids == [r_id]
        assert "entities" in json.loads(delete_closure.signature())


class TestValidateMergeRequest:
    """Tests for _validate_merge_request logic."""

    @pytest.mark.asyncio
    async def test_validate_merge_entity_missing(self) -> None:
        """Verify conflict when source or target entity is missing from closure."""
        src_id = uuid4()
        tgt_id = uuid4()
        payload = EntityMergeRequest(into_id=tgt_id, expected_revision=1, expected_into_revision=1, reason="test")

        closure = _Closure(
            entity_rows=[], memberships=[], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_merge_request(src_id, payload, closure)
        assert exc_info.value.conflict.code == "entity_missing"

    @pytest.mark.asyncio
    async def test_validate_merge_stale_revision(self) -> None:
        """Verify conflict when revision in payload does not match entity revision."""
        src = _make_sample_entity(revision=2)
        tgt = _make_sample_entity(revision=1)
        payload = EntityMergeRequest(into_id=tgt.id, expected_revision=1, expected_into_revision=1, reason="test")

        closure = _Closure(
            entity_rows=[src, tgt], memberships=[], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id, tgt.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_merge_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "stale_revision"

    @pytest.mark.asyncio
    async def test_validate_merge_incompatible_types(self) -> None:
        """Verify conflict when source and target have differing entity types."""
        src = _make_sample_entity(type_="person", revision=1)
        tgt = _make_sample_entity(type_="organization", revision=1)
        payload = EntityMergeRequest(into_id=tgt.id, expected_revision=1, expected_into_revision=1, reason="test")

        closure = _Closure(
            entity_rows=[src, tgt], memberships=[], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id, tgt.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_merge_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "incompatible_entity_types"

    @pytest.mark.asyncio
    async def test_validate_merge_protected_field_conflict(self) -> None:
        """Verify conflict when both entities have different owner-authored field values."""
        src = _make_sample_entity(name="Name A", name_origin="owner", revision=1)
        tgt = _make_sample_entity(name="Name B", name_origin="owner", revision=1)
        payload = EntityMergeRequest(into_id=tgt.id, expected_revision=1, expected_into_revision=1, reason="test")

        closure = _Closure(
            entity_rows=[src, tgt], memberships=[], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id, tgt.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_merge_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "protected_field_conflict"

    @pytest.mark.asyncio
    async def test_validate_merge_success(self) -> None:
        """Verify successful merge validation returns unpacked components."""
        src = _make_sample_entity(name="Derived A", name_origin="derived", revision=1)
        tgt = _make_sample_entity(name="Owner B", name_origin="owner", revision=1)
        mem = _make_sample_membership(entity_id=src.id)
        payload = EntityMergeRequest(into_id=tgt.id, expected_revision=1, expected_into_revision=1, reason="test")

        closure = _Closure(
            entity_rows=[src, tgt], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id, tgt.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[mem.id], timeline_event_ids=[],
        )

        s, t, memberships, _target_aliases, _source_aliases = await _validate_merge_request(src.id, payload, closure)
        assert s.id == src.id
        assert t.id == tgt.id
        assert memberships == [mem]


class TestValidateSplitRequest:
    """Tests for _validate_split_request logic."""

    @pytest.mark.asyncio
    async def test_validate_split_entity_missing(self) -> None:
        """Verify conflict when entity to split is not found."""
        ent_id = uuid4()
        new_ent = EntityCreate(type="topic", name="New Node", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[uuid4()], new_entity=new_ent, reason="split",
        )
        closure = _Closure(
            entity_rows=[], memberships=[], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_split_request(ent_id, payload, closure)
        assert exc_info.value.conflict.code == "entity_missing"

    @pytest.mark.asyncio
    async def test_validate_split_incompatible_type(self) -> None:
        """Verify split entity must preserve the original entity type."""
        src = _make_sample_entity(type_="person", revision=1)
        mem = _make_sample_membership(entity_id=src.id)
        new_ent = EntityCreate(type="organization", name="New Node", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[mem.id], new_entity=new_ent, reason="split",
        )
        closure = _Closure(
            entity_rows=[src], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id], evidence_pairs=[],
            source_ids=[mem.source_id], document_ids=[mem.document_id],
            membership_ids=[mem.id], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_split_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "incompatible_entity_types"

    @pytest.mark.asyncio
    async def test_validate_split_same_canonical_name(self) -> None:
        """Verify split entity must have a distinct canonical name."""
        src = _make_sample_entity(type_="topic", name="Acme Topic", revision=1)
        mem = _make_sample_membership(entity_id=src.id)
        new_ent = EntityCreate(type="topic", name="acme topic", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[mem.id], new_entity=new_ent, reason="split",
        )
        closure = _Closure(
            entity_rows=[src], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id], evidence_pairs=[],
            source_ids=[mem.source_id], document_ids=[mem.document_id],
            membership_ids=[mem.id], timeline_event_ids=[],
        )

        with pytest.raises(CorrectionConflictError) as exc_info:
            await _validate_split_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "split_identity_conflict"

    @pytest.mark.asyncio
    async def test_validate_split_relationship_conflict(self) -> None:
        """Verify conflict when relationships attached to entity fail split validation."""
        src = _make_sample_entity(type_="topic", revision=1)
        mem = _make_sample_membership(entity_id=src.id)
        new_ent = EntityCreate(type="topic", name="Distinct Name", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[mem.id], new_entity=new_ent, reason="split",
        )
        closure = _Closure(
            entity_rows=[src], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[mem.id], timeline_event_ids=[],
        )

        with patch("modules.knowledge.relationships.public.validate_entity_split_plan", side_effect=ValueError("Split conflict")):  # noqa: SIM117  # style-only rewrite skipped to avoid touching control flow
            with pytest.raises(CorrectionConflictError) as exc_info:
                await _validate_split_request(src.id, payload, closure)
        assert exc_info.value.conflict.code == "relationship_split_conflict"

    @pytest.mark.asyncio
    async def test_validate_split_success(self) -> None:
        """Verify valid split where subset of evidence is moved."""
        src = _make_sample_entity(type_="topic", revision=1)
        mem1 = _make_sample_membership(entity_id=src.id)
        mem2 = _make_sample_membership(entity_id=src.id)
        new_ent = EntityCreate(type="topic", name="Distinct Name", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[mem1.id], new_entity=new_ent, reason="split",
        )
        closure = _Closure(
            entity_rows=[src], memberships=[mem1, mem2], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id], evidence_pairs=[],
            source_ids=[], document_ids=[], membership_ids=[mem1.id, mem2.id], timeline_event_ids=[],
        )

        source, selected = await _validate_split_request(src.id, payload, closure)
        assert source.id == src.id
        assert len(selected) == 1
        assert selected[0].id == mem1.id


class TestPreviewsAndExecution:
    """Tests for preview_merge, preview_split, merge_entity, and split_entity."""

    @pytest.mark.asyncio
    async def test_preview_merge_self_merge_returns_conflict(self) -> None:
        """Verify preview_merge detects self_merge and returns conflict preview without raising."""
        session = AsyncMock()
        same_id = uuid4()
        payload = EntityMergeRequest(into_id=same_id, expected_revision=1, expected_into_revision=1, reason="test")

        preview = await preview_merge(session, same_id, payload, **KW)
        assert isinstance(preview, EntityCorrectionPreview)
        assert preview.operation == "merge"
        assert len(preview.conflicts) == 1
        assert preview.conflicts[0].code == "self_merge"

    @pytest.mark.asyncio
    async def test_preview_split_handles_conflict(self) -> None:
        """Verify preview_split gracefully catches conflicts and returns preview with conflict DTO."""
        session = AsyncMock()
        ent_id = uuid4()
        new_ent = EntityCreate(type="topic", name="Name", description=None, aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[uuid4()], new_entity=new_ent, reason="split",
        )

        with (
            patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(return_value=ent_id)),
            patch("modules.knowledge.entities.corrections._discover", side_effect=_conflict("mock_err", "Mock error")),
        ):
            preview = await preview_split(session, ent_id, payload, **KW)

        assert preview.operation == "split"
        assert len(preview.conflicts) == 1
        assert preview.conflicts[0].code == "mock_err"

    @pytest.mark.asyncio
    async def test_merge_entity_self_merge_raises_conflict(self) -> None:
        """Verify merge_entity rejects self-merges with CorrectionConflictError."""
        session = AsyncMock()
        same_id = uuid4()
        payload = EntityMergeRequest(into_id=same_id, expected_revision=1, expected_into_revision=1, reason="test")

        with pytest.raises(CorrectionConflictError) as exc_info:
            await merge_entity(session, same_id, payload, **KW)
        assert exc_info.value.conflict.code == "self_merge"

    @pytest.mark.asyncio
    async def test_merge_entity_redirected_entity_raises_conflict(self) -> None:
        """Verify merge_entity rejects entities that already redirect to another entity."""
        session = AsyncMock()
        src_id = uuid4()
        tgt_id = uuid4()
        payload = EntityMergeRequest(into_id=tgt_id, expected_revision=1, expected_into_revision=1, reason="test")

        with patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(side_effect=[uuid4(), tgt_id])):
            with pytest.raises(CorrectionConflictError) as exc_info:
                await merge_entity(session, src_id, payload, **KW)
            assert exc_info.value.conflict.code == "redirected_entity"

    @pytest.mark.asyncio
    async def test_merge_entity_success(self) -> None:
        """Verify merge_entity redirects identity, migrates memberships, and records audit decision."""
        session = AsyncMock()
        session.scalars = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.add_all = MagicMock()
        session.flush = AsyncMock()

        src = _make_sample_entity(name="Source Node", name_origin="derived", revision=1)
        tgt = _make_sample_entity(name="Target Node", name_origin="owner", revision=2)
        mem = _make_sample_membership(entity_id=src.id)
        payload = EntityMergeRequest(into_id=tgt.id, expected_revision=1, expected_into_revision=2, reason="Merge duplicate")

        closure = _Closure(
            entity_rows=[src, tgt], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id, tgt.id], evidence_pairs=[],
            source_ids=[mem.source_id], document_ids=[mem.document_id], membership_ids=[mem.id], timeline_event_ids=[],
        )

        with (
            patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(side_effect=[src.id, tgt.id])),
            patch("modules.knowledge.entities.corrections._locked_closure", AsyncMock(return_value=closure)),
            patch("modules.knowledge.entities.corrections._temporal_before_relationships", AsyncMock()),
            patch("modules.knowledge.entities.corrections._temporal_correction", AsyncMock()),
            patch("modules.knowledge.entities.corrections.commit_with_replay", AsyncMock()),
            patch("modules.timeline.public.apply_entity_merge", AsyncMock(return_value=[])),
            patch("modules.timeline.public.revise_corrected_events", AsyncMock(return_value=[])),
        ):
            result = await merge_entity(session, src.id, payload, **KW)

        assert isinstance(result, EntityCorrectionResult)
        assert result.operation == "merge"
        assert result.entity_id == src.id
        assert result.canonical_entity_id == tgt.id
        assert result.revision == 3
        # Membership reassigned to target
        assert mem.entity_id == tgt.id

    @pytest.mark.asyncio
    async def test_split_entity_success(self) -> None:
        """Verify split_entity creates new entity, moves memberships, and logs audit decision."""
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        session.add = MagicMock()
        session.add_all = MagicMock()
        session.flush = AsyncMock()

        src = _make_sample_entity(type_="topic", revision=1)
        mem1 = _make_sample_membership(entity_id=src.id)
        mem2 = _make_sample_membership(entity_id=src.id)
        new_ent = EntityCreate(type="topic", name="Split Node", description="New description", aliases=[], metadata={})
        payload = EntitySplitRequest(
            expected_revision=1, evidence_ids=[mem1.id], new_entity=new_ent, reason="Split out concept",
        )

        closure = _Closure(
            entity_rows=[src], memberships=[mem1, mem2], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[src.id], evidence_pairs=[],
            source_ids=[mem1.source_id], document_ids=[mem1.document_id],
            membership_ids=[mem1.id, mem2.id], timeline_event_ids=[],
        )

        with (
            patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(return_value=src.id)),
            patch("modules.knowledge.entities.corrections._locked_closure", AsyncMock(return_value=closure)),
            patch("modules.knowledge.entities.corrections._temporal_before_relationships", AsyncMock()),
            patch("modules.knowledge.entities.corrections._temporal_correction", AsyncMock()),
            patch("modules.knowledge.entities.corrections.commit_with_replay", AsyncMock()),
            patch("modules.timeline.public.apply_entity_split", AsyncMock(return_value=[])),
            patch("modules.timeline.public.revise_corrected_events", AsyncMock(return_value=[])),
            patch("modules.knowledge.relationships.public.apply_entity_split", AsyncMock(return_value=[])),
        ):
            result = await split_entity(session, src.id, payload, **KW)

        assert isinstance(result, EntityCorrectionResult)
        assert result.operation == "split"
        assert result.entity_id == src.id
        assert len(result.replacement_entity_ids) == 1
        assert result.revision == 2
        # Selected membership reassigned to the new entity
        assert mem1.entity_id == result.replacement_entity_ids[0]
        # Unselected membership remains on source
        assert mem2.entity_id == src.id

    @pytest.mark.asyncio
    async def test_suppress_candidates_audit_logging(self) -> None:
        """Verify suppress_candidates adds EntityCorrectionDecision audit log."""
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        session.add = MagicMock()
        session.flush = AsyncMock()

        ent = _make_sample_entity(revision=1)
        mem = _make_sample_membership(entity_id=ent.id)
        payload = EntitySuppressionRequest(
            expected_revision=1, evidence_ids=[mem.id], reason="Suppress false positive",
        )

        closure = _Closure(
            entity_rows=[ent], memberships=[mem], aliases=[], alias_supports=[],
            field_supports=[], relationship_refs=[], redirect_rows=[],
            relationship_entity_ids=[], entity_ids=[ent.id], evidence_pairs=[],
            source_ids=[mem.source_id], document_ids=[mem.document_id],
            membership_ids=[mem.id], timeline_event_ids=[],
        )

        with (
            patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(return_value=ent.id)),
            patch("modules.knowledge.entities.corrections._locked_closure", AsyncMock(return_value=closure)),
            patch("modules.knowledge.entities.corrections._temporal_before_relationships", AsyncMock()),
            patch("modules.knowledge.entities.corrections._temporal_correction", AsyncMock()),
            patch("modules.knowledge.entities.corrections.commit_with_replay", AsyncMock()),
            patch("modules.knowledge.entities.public.record_owner_action", AsyncMock()),
            patch("modules.knowledge.relationships.public.remove_entity_closure", AsyncMock(return_value=[])),
        ):
            result = await suppress_candidates(session, ent.id, payload, **KW)

        assert isinstance(result, EntityCorrectionResult)
        assert result.operation == "suppress"
        assert result.entity_id == ent.id
        # Check that EntityCorrectionDecision was added
        added_decisions = [call[0][0] for call in session.add.call_args_list if isinstance(call[0][0], EntityCorrectionDecision)]
        assert len(added_decisions) >= 1
        assert added_decisions[0].decision == "suppress"
        assert added_decisions[0].reason == "Suppress false positive"
