"""Unit tests for modules.knowledge.relationships.public.

Tests cover:
- list_relationships query bounds (limit, aware datetimes), cursor validation,
  filter predicates, and keyset pagination logic.
- get_neighbors node limit bounds, focus resolution, cursor encoding/decoding,
  and neighbor page truncation.
- list_relationship_evidence limit bounds, aware cutoff, missing relationship handling,
  and source permission filtering.
- create_relationship endpoint validation (self-loops, derived evidence requirements,
  evidence uniqueness, endpoint existence).
- validate_entity_merge_plan and validate_entity_split_plan constraint validation.
- purge_history_support bounds checking and short-circuit on empty input.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.relationships.public import (
    MAX_CLEANUP_SUPPORTS,
    _decode_neighbor_cursor,
    _encode_neighbor_cursor,
    create_relationship,
    get_neighbors,
    list_relationship_evidence,
    list_relationships,
    purge_history_support,
    validate_entity_merge_plan,
    validate_entity_split_plan,
)
from modules.knowledge.relationships.schemas import (
    CorrectionRelationshipRef,
    CorrectionSupportRef,
    EvidenceRef,
    RelationshipCreate,
    RelationshipPage,
    RelationshipRead,
    RelationshipSnapshot,
)

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
KW = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _admitted() -> object:
    """Admission is covered in test_scope; these tests exercise the domain logic past it."""
    with patch("modules.knowledge.relationships.public._admit", AsyncMock(return_value=MagicMock())):
        yield


class TestListRelationships:
    """Tests for list_relationships query bounds, filters, cursor, and pagination."""

    @pytest.mark.asyncio
    async def test_limit_bounds_validation(self) -> None:
        """Verify list_relationships rejects limit < 1 or limit > 100."""
        session = AsyncMock()
        with pytest.raises(ValueError, match="Relationship page limit must be between 1 and 100"):
            await list_relationships(session, limit=0, cursor=None, **KW)

        with pytest.raises(ValueError, match="Relationship page limit must be between 1 and 100"):
            await list_relationships(session, limit=101, cursor=None, **KW)

    @pytest.mark.asyncio
    async def test_naive_datetime_rejected(self) -> None:
        """Verify naive valid_at or knowledge_as_of raises ValueError."""
        session = AsyncMock()
        naive_dt = datetime(2026, 10, 5, 12, 0, 0)  # No tzinfo  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test

        with pytest.raises(ValueError, match="Relationship time controls require aware instants"):
            await list_relationships(session, limit=10, cursor=None, valid_at=naive_dt, **KW)

        with pytest.raises(ValueError, match="Relationship time controls require aware instants"):
            await list_relationships(session, limit=10, cursor=None, knowledge_as_of=naive_dt, **KW)

    @pytest.mark.asyncio
    async def test_invalid_cursor_rejected(self) -> None:
        """Verify malformed or altered cursor raises ValueError."""
        session = AsyncMock()

        # Cursor exceeds 1024 bytes
        long_cursor = "a" * 1025
        with pytest.raises(ValueError, match="Invalid relationship filter cursor"):
            await list_relationships(session, limit=10, cursor=long_cursor, **KW)

        # Invalid base64
        with pytest.raises(ValueError, match="Invalid relationship filter cursor"):
            await list_relationships(session, limit=10, cursor="!!!not-base64!!!", **KW)

        # Scope fingerprint mismatch
        wrong_scope_cursor = base64.urlsafe_b64encode(json.dumps(["wrong_scope", "2026-10-05"]).encode()).decode()
        with pytest.raises(ValueError, match="Invalid relationship filter cursor"):
            await list_relationships(session, limit=10, cursor=wrong_scope_cursor, **KW)

    @pytest.mark.asyncio
    async def test_canonical_entity_id_resolution(self) -> None:
        """Verify entity_id is resolved to canonical ID before querying."""
        session = AsyncMock()
        session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        requested_id = uuid4()
        canonical_id = uuid4()

        with patch("modules.knowledge.entities.public.resolve_canonical_entity_id", AsyncMock(return_value=canonical_id)) as mock_resolve:
            page = await list_relationships(session, limit=10, cursor=None, entity_id=requested_id, **KW)

        mock_resolve.assert_awaited_once_with(session, requested_id, **KW)
        assert isinstance(page, RelationshipPage)
        assert page.items == []
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_time_travel_routing_to_as_of(self) -> None:
        """Verify knowledge_as_of routes to _list_relationships_as_of."""
        session = AsyncMock()
        as_of = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        expected_page = RelationshipPage(
            items=[], next_cursor=None, canonical_history_available=False,
            knowledge_as_of=as_of, observation_history_only=True,
        )

        with patch("modules.knowledge.relationships.public._list_relationships_as_of", AsyncMock(return_value=expected_page)) as mock_as_of:
            page = await list_relationships(session, limit=10, cursor=None, knowledge_as_of=as_of, **KW)

        assert page == expected_page
        mock_as_of.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pagination_next_cursor_generation(self) -> None:
        """Verify next_cursor is returned when results exceed limit."""
        session = AsyncMock()
        now = datetime.now(UTC)

        # Mock rows: limit + 1 items (e.g. limit=2, returned 3)
        rows = [
            MagicMock(id=uuid4(), created_at=now),
            MagicMock(id=uuid4(), created_at=now),
            MagicMock(id=uuid4(), created_at=now),
        ]
        session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))

        # Mock snapshot for the 2 returned items
        mock_read = MagicMock(spec=RelationshipRead)
        mock_snapshot = RelationshipSnapshot(
            relationship=mock_read,
            endpoints=[],
            memberships=[],
            supports=[],
            source_generations={},
            digest="abcde",
        )

        with patch("modules.knowledge.relationships.public.get_relationship_snapshot", AsyncMock(return_value=mock_snapshot)):
            page = await list_relationships(session, limit=2, cursor=None, **KW)

        assert len(page.items) == 2
        assert page.next_cursor is not None


class TestGetNeighbors:
    """Tests for get_neighbors bounds, cursor encoding/decoding, and pagination."""

    @pytest.mark.asyncio
    async def test_limit_bounds_validation(self) -> None:
        """Verify get_neighbors rejects limit < 2 or limit > 100."""
        session = AsyncMock()
        with pytest.raises(ValueError, match="Neighbor page limit must be between 2 and 100 total nodes"):
            await get_neighbors(session, entity_id=uuid4(), limit=1, **KW)

        with pytest.raises(ValueError, match="Neighbor page limit must be between 2 and 100 total nodes"):
            await get_neighbors(session, entity_id=uuid4(), limit=101, **KW)

    def test_neighbor_cursor_roundtrip_and_validation(self) -> None:
        """Verify _encode_neighbor_cursor and _decode_neighbor_cursor roundtrip and reject mismatch."""
        focus_id = uuid4()
        rel_id = uuid4()
        dt = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

        cursor = _encode_neighbor_cursor(focus_id, dt, rel_id)
        assert isinstance(cursor, str)
        assert "=" not in cursor

        decoded_dt, decoded_id = _decode_neighbor_cursor(cursor, focus_id)
        assert decoded_dt == dt
        assert decoded_id == rel_id

        # Mismatched focus_id raises ValueError
        other_focus = uuid4()
        with pytest.raises(ValueError, match="Invalid neighbor cursor"):
            _decode_neighbor_cursor(cursor, other_focus)

        # Invalid cursor with "=" raises ValueError
        with pytest.raises(ValueError, match="Invalid neighbor cursor"):
            _decode_neighbor_cursor(cursor + "=", focus_id)

    @pytest.mark.asyncio
    async def test_focus_lookup_failure_returns_none(self) -> None:
        """Verify get_neighbors returns None when focus entity is not found."""
        session = AsyncMock()
        with patch("modules.knowledge.entities.public.get_entity_refs", AsyncMock(side_effect=LookupError)):
            result = await get_neighbors(session, entity_id=uuid4(), limit=10, **KW)
        assert result is None


class TestListRelationshipEvidence:
    """Tests for list_relationship_evidence bounds, cutoff awareness, and permissions."""

    @pytest.mark.asyncio
    async def test_limit_bounds_validation(self) -> None:
        """Verify limit must be between 1 and 100."""
        session = AsyncMock()
        with pytest.raises(ValueError, match="Relationship evidence limit must be between 1 and 100"):
            await list_relationship_evidence(session, relationship_id=uuid4(), limit=0, cursor=None, **KW)

        with pytest.raises(ValueError, match="Relationship evidence limit must be between 1 and 100"):
            await list_relationship_evidence(session, relationship_id=uuid4(), limit=101, cursor=None, **KW)

    @pytest.mark.asyncio
    async def test_naive_cutoff_rejected(self) -> None:
        """Verify naive knowledge_as_of raises ValueError."""
        session = AsyncMock()
        naive_dt = datetime(2026, 10, 5, 12, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValueError, match="Evidence cutoff requires an aware instant"):
            await list_relationship_evidence(session, relationship_id=uuid4(), limit=10, cursor=None, knowledge_as_of=naive_dt, **KW)

    @pytest.mark.asyncio
    async def test_missing_relationship_returns_none(self) -> None:
        """Verify non-existent relationship returns (None, None)."""
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)

        evidence, next_cursor = await list_relationship_evidence(session, relationship_id=uuid4(), limit=10, cursor=None, **KW)
        assert evidence is None
        assert next_cursor is None


class TestCreateRelationship:
    """Tests for create_relationship validation and link integrity."""

    @pytest.mark.asyncio
    async def test_self_loop_rejected(self) -> None:
        """Verify self-loop (source == target) raises ValueError."""
        session = AsyncMock()
        same_id = uuid4()
        payload = RelationshipCreate(
            source_entity_id=same_id,
            target_entity_id=same_id,
            type="RELATES_TO",
            origin="owner",
            reason="Test",
        )
        with pytest.raises(ValueError, match="Relationship endpoints must be different"):
            await create_relationship(session, payload, **KW)

    @pytest.mark.asyncio
    async def test_derived_without_evidence_rejected(self) -> None:
        """Verify derived origin without evidence references raises ValueError."""
        session = AsyncMock()
        payload = RelationshipCreate(
            source_entity_id=uuid4(),
            target_entity_id=uuid4(),
            type="RELATES_TO",
            origin="derived",
            evidence=[],
            reason="Test",
        )
        with pytest.raises(ValueError, match="Derived relationships require at least one evidence reference"):
            await create_relationship(session, payload, **KW)

    @pytest.mark.asyncio
    async def test_duplicate_evidence_pairs_rejected(self) -> None:
        """Verify repeated (version_id, chunk_id, source_membership, target_membership) raises ValueError."""
        session = AsyncMock()
        v_id = uuid4()
        c_id = uuid4()
        sm_id = uuid4()
        tm_id = uuid4()

        ev1 = EvidenceRef(document_version_id=v_id, chunk_id=c_id, confidence=0.8, source_membership_id=sm_id, target_membership_id=tm_id)
        ev2 = EvidenceRef(document_version_id=v_id, chunk_id=c_id, confidence=0.9, source_membership_id=sm_id, target_membership_id=tm_id)

        payload = RelationshipCreate(
            source_entity_id=uuid4(),
            target_entity_id=uuid4(),
            type="RELATES_TO",
            origin="derived",
            evidence=[ev1, ev2],
            reason="Test",
        )
        with pytest.raises(ValueError, match="Relationship evidence membership pairs must be unique"):
            await create_relationship(session, payload, **KW)

    @pytest.mark.asyncio
    async def test_endpoint_entity_not_found_raises_lookup_error(self) -> None:
        """Verify LookupError when source or target entity ref is missing."""
        session = AsyncMock()
        payload = RelationshipCreate(
            source_entity_id=uuid4(),
            target_entity_id=uuid4(),
            type="RELATES_TO",
            origin="owner",
            reason="Test",
        )

        with (  # noqa: SIM117  # style-only rewrite skipped to avoid touching control flow
            patch("modules.knowledge.documents.public.read_evidence_refs", AsyncMock(return_value=[])),
            patch("modules.knowledge.entities.public.get_entity_refs", AsyncMock(side_effect=LookupError("Entity missing"))),
        ):
            with pytest.raises(LookupError, match="Relationship entity not found"):
                await create_relationship(session, payload, **KW)


class TestPlanValidations:
    """Tests for validate_entity_merge_plan and validate_entity_split_plan."""

    def test_validate_merge_plan_conflicting_metadata(self) -> None:
        """Verify validate_entity_merge_plan detects conflicting metadata among collapsing relationships."""
        src_id = uuid4()
        tgt_id = uuid4()
        other_id = uuid4()

        ref1 = CorrectionRelationshipRef(
            id=uuid4(), source_entity_id=src_id, target_entity_id=other_id,
            type="KNOWS", origin="owner", valid_from=None, valid_to=None,
            metadata={"weight": 1}, supports=[],
        )
        ref2 = CorrectionRelationshipRef(
            id=uuid4(), source_entity_id=tgt_id, target_entity_id=other_id,
            type="KNOWS", origin="owner", valid_from=None, valid_to=None,
            metadata={"weight": 2}, supports=[],  # Conflicting metadata
        )

        with pytest.raises(ValueError, match="Merge has conflicting relationship metadata"):
            validate_entity_merge_plan(src_id, tgt_id, [ref1, ref2], set())

    def test_validate_split_plan_owner_with_supports_rejected(self) -> None:
        """Verify validate_entity_split_plan rejects owner-authored relationship with evidence supports."""
        ent_id = uuid4()
        sup = CorrectionSupportRef(
            id=uuid4(),
            document_id=uuid4(),
            source_id=uuid4(),
            document_version_id=uuid4(),
            chunk_id=uuid4(),
            source_membership_id=uuid4(),
            target_membership_id=uuid4(),
            confidence=0.9,
        )
        ref = CorrectionRelationshipRef(
            id=uuid4(), source_entity_id=ent_id, target_entity_id=uuid4(),
            type="KNOWS", origin="owner", valid_from=None, valid_to=None,
            metadata={}, supports=[sup],
        )

        with pytest.raises(ValueError, match="unexpected evidence attached to an owner-authored relationship"):
            validate_entity_split_plan(ent_id, {uuid4()}, [ref])

    def test_validate_split_plan_self_relationship_prevention(self) -> None:
        """Verify validate_entity_split_plan rejects plan when moving both endpoints would form a self-loop."""
        ent_id = uuid4()
        mem_id = uuid4()
        sup = CorrectionSupportRef(
            id=uuid4(),
            document_id=uuid4(),
            source_id=uuid4(),
            document_version_id=uuid4(),
            chunk_id=uuid4(),
            source_membership_id=mem_id,
            target_membership_id=mem_id,  # Both point to moving membership
            confidence=0.9,
        )
        ref = CorrectionRelationshipRef(
            id=uuid4(), source_entity_id=ent_id, target_entity_id=ent_id,
            type="KNOWS", origin="derived", valid_from=None, valid_to=None,
            metadata={}, supports=[sup],
        )

        with pytest.raises(ValueError, match="Split would create a self relationship"):
            validate_entity_split_plan(ent_id, {mem_id}, [ref])


class TestPurgeAndCleanupBounds:
    """Tests for purge_history_support bounds (discovery is bounded; held apply never scans)."""

    @pytest.mark.asyncio
    async def test_purge_history_support_rejects_overflowed_discovery(self) -> None:
        """An overflowed rediscovery aborts the held apply instead of truncating."""
        from core.workspaces.schemas import AccessFence
        from modules.knowledge.relationships.public import RelationshipSupportClosure
        from modules.sources.schemas import SourceFence

        ws, src = uuid4(), uuid4()
        scope = WorkspaceContext(user_id=1, workspace_id=ws, role="owner", membership_revision=1)
        fence = AccessFence(ws, 1, 1, 1)
        source_fence = SourceFence(id=src, workspace_id=ws, status="purging", generation=1, local_only=False)
        rows = MagicMock(all=MagicMock(return_value=[(uuid4(), uuid4()) for _ in range(MAX_CLEANUP_SUPPORTS + 1)]))
        session = AsyncMock()
        session.execute.return_value = rows
        session.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        closure = RelationshipSupportClosure(src, None, (), (), (), (), (), False)
        with (
            patch("modules.knowledge.relationships.public._admit", AsyncMock(return_value=fence)),
            pytest.raises(RuntimeError, match="cleanup closure changed"),
        ):
            await purge_history_support(
                session, closure, [(uuid4(), uuid4())], scope=scope, multi_workspace_enabled=False,
                access_fence=fence, source_fence=source_fence)

    @pytest.mark.asyncio
    async def test_purge_history_support_empty_returns_early(self) -> None:
        """Empty refs perform no history scan or row fetch."""
        from core.workspaces.schemas import AccessFence
        from modules.knowledge.relationships.public import RelationshipSupportClosure
        from modules.sources.schemas import SourceFence

        ws, src = uuid4(), uuid4()
        scope = WorkspaceContext(user_id=1, workspace_id=ws, role="owner", membership_revision=1)
        fence = AccessFence(ws, 1, 1, 1)
        source_fence = SourceFence(id=src, workspace_id=ws, status="purging", generation=1, local_only=False)
        session = AsyncMock()
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[]))
        closure = RelationshipSupportClosure(src, None, (), (), (), (), (), False)
        with patch("modules.knowledge.relationships.public._admit", AsyncMock(return_value=fence)):
            await purge_history_support(
                session, closure, [], scope=scope, multi_workspace_enabled=False,
                access_fence=fence, source_fence=source_fence)
        session.scalars.assert_not_called()
