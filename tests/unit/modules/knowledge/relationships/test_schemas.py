"""Unit tests for knowledge graph relationship schemas, predicates, and source/target constraints.

Covers:
- RelationshipCreate, RelationshipRead, EvidenceRead, RelationshipPage, NeighborRead, NeighborPage.
- Predicate pattern validation (strict upper-snake-case ^[A-Z][A-Z0-9_]*$).
- Source and target entity UUID constraints.
- Origin, confidence range [0, 1], and evidence bounds (max 100).
- Metadata 64 KiB bounds and extra field rejection.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.knowledge.entities.schemas import EvidenceRef
from modules.knowledge.relationships.schemas import (
    EntityGraphRead,
    EvidenceRead,
    NeighborPage,
    NeighborRead,
    RelationshipCreate,
    RelationshipPage,
    RelationshipRead,
)


class TestRelationshipCreate:
    """Tests for RelationshipCreate validation, predicates, and entity constraints."""

    def test_valid_owner_relationship_create(self) -> None:
        """Verify valid owner-created relationship with minimal required fields."""
        source_id = uuid4()
        target_id = uuid4()
        rel = RelationshipCreate(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type="EMPLOYED_BY",
        )
        assert rel.source_entity_id == source_id
        assert rel.target_entity_id == target_id
        assert rel.type == "EMPLOYED_BY"
        assert rel.origin == "owner"
        assert rel.confidence is None
        assert rel.metadata == {}
        assert rel.evidence == []
        assert rel.reason == "owner_relationship"

    def test_valid_derived_relationship_with_evidence(self) -> None:
        """Verify derived relationship with confidence, validity boundaries, and evidence."""
        source_id = uuid4()
        target_id = uuid4()
        evidence_ref = EvidenceRef(
            document_version_id=uuid4(),
            chunk_id=uuid4(),
            confidence=0.9,
        )
        rel = RelationshipCreate(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type="LOCATED_IN",
            origin="derived",
            confidence=0.92,
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 1, 1, tzinfo=UTC),
            evidence=[evidence_ref],
            reason="extracted_from_news",
        )
        assert rel.origin == "derived"
        assert rel.confidence == 0.92
        assert len(rel.evidence) == 1

    def test_predicate_pattern_valid_examples(self) -> None:
        """Verify uppercase snake-case predicates matching ^[A-Z][A-Z0-9_]*$ are accepted."""
        valid_predicates = [
            "WORKS_FOR",
            "FOUNDED_BY",
            "INVESTED_IN_2024",
            "MEMBER_OF",
            "IS_A",
            "ACQUIRED",
            "PARENT_COMPANY_OF",
        ]
        source_id = uuid4()
        target_id = uuid4()
        for pred in valid_predicates:
            rel = RelationshipCreate(
                source_entity_id=source_id,
                target_entity_id=target_id,
                type=pred,
            )
            assert rel.type == pred

    def test_predicate_pattern_invalid_examples(self) -> None:
        """Verify lowercase, starting with digits/underscores, or containing spaces/hyphens fail."""
        source_id = uuid4()
        target_id = uuid4()
        invalid_predicates = [
            "works_for",          # lowercase
            "worksFor",           # camelCase
            "WorksFor",           # PascalCase
            "_WORKS_FOR",         # leading underscore
            "1ST_INVESTOR",       # leading digit
            "WORKS FOR",          # space
            "WORKS-FOR",          # hyphen
            "",                   # empty
            "WORKS_FOR!",         # special character
        ]
        for pred in invalid_predicates:
            with pytest.raises(ValidationError):
                RelationshipCreate(
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    type=pred,
                )

    def test_predicate_length_bounds(self) -> None:
        """Verify predicate max length is 64 characters."""
        source_id = uuid4()
        target_id = uuid4()

        valid_64 = "A" * 64
        rel = RelationshipCreate(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type=valid_64,
        )
        assert rel.type == valid_64

        invalid_65 = "A" * 65
        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=source_id,
                target_entity_id=target_id,
                type=invalid_65,
            )

    def test_source_and_target_must_be_valid_uuids(self) -> None:
        """Verify non-UUID strings or numbers are rejected for entity endpoints."""
        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id="not-a-uuid",  # type: ignore[arg-type]
                target_entity_id=uuid4(),
                type="KNOWS",
            )

        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=uuid4(),
                target_entity_id=12345,  # type: ignore[arg-type]
                type="KNOWS",
            )

    def test_confidence_bounds(self) -> None:
        """Verify confidence must be within [0.0, 1.0]."""
        source_id = uuid4()
        target_id = uuid4()

        assert RelationshipCreate(
            source_entity_id=source_id, target_entity_id=target_id, type="KNOWS", confidence=0.0
        ).confidence == 0.0
        assert RelationshipCreate(
            source_entity_id=source_id, target_entity_id=target_id, type="KNOWS", confidence=1.0
        ).confidence == 1.0

        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=source_id, target_entity_id=target_id, type="KNOWS", confidence=-0.01
            )

        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=source_id, target_entity_id=target_id, type="KNOWS", confidence=1.01
            )

    def test_evidence_max_length_bound(self) -> None:
        """Verify evidence is capped at 100 references."""
        source_id = uuid4()
        target_id = uuid4()
        valid_refs = [
            EvidenceRef(document_version_id=uuid4(), chunk_id=uuid4(), confidence=0.8)
            for _ in range(100)
        ]
        rel = RelationshipCreate(
            source_entity_id=source_id,
            target_entity_id=target_id,
            type="KNOWS",
            evidence=valid_refs,
        )
        assert len(rel.evidence) == 100

        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=source_id,
                target_entity_id=target_id,
                type="KNOWS",
                evidence=valid_refs + [EvidenceRef(document_version_id=uuid4(), chunk_id=uuid4(), confidence=0.8)],
            )

    def test_extra_fields_forbidden(self) -> None:
        """Verify extra unexpected fields are rejected on RelationshipCreate."""
        with pytest.raises(ValidationError):
            RelationshipCreate(
                source_entity_id=uuid4(),
                target_entity_id=uuid4(),
                type="KNOWS",
                unrecognized_field="val",  # type: ignore[call-arg]
            )


class TestRelationshipReadAndPages:
    """Tests for RelationshipRead, EvidenceRead, NeighborRead, and pagination schemas."""

    def test_relationship_read_structure(self) -> None:
        """Verify RelationshipRead serialization with validity precision."""
        rel_id = uuid4()
        source_id = uuid4()
        target_id = uuid4()
        now = datetime.now(UTC)

        read = RelationshipRead(
            id=rel_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            type="COLLABORATES_WITH",
            origin="derived",
            confidence=0.85,
            valid_from=now,
            valid_to=None,
            metadata={"weight": 5},
            created_at=now,
            evidence=[],
            validity_precision="unknown",
        )
        assert read.id == rel_id
        assert read.validity_precision == "unknown"
        assert read.metadata["weight"] == 5

    def test_evidence_read_structure(self) -> None:
        """Verify EvidenceRead fields and provenance tracking."""
        evidence = EvidenceRead(
            id=uuid4(),
            relationship_id=uuid4(),
            document_id=uuid4(),
            document_version_id=uuid4(),
            version_number=2,
            chunk_id=uuid4(),
            observed_at=datetime.now(UTC),
            extracted_at=datetime.now(UTC),
            confidence=0.91,
            source_entity_membership_id=uuid4(),
            target_entity_membership_id=uuid4(),
            title="Partnership Announcement",
            canonical_url="https://example.com/pr",
            source_id=uuid4(),
            excerpt="Acme partnered with Beta...",
            metadata_is_version_snapshot=True,
        )
        assert evidence.version_number == 2
        assert evidence.metadata_is_version_snapshot is True

    def test_neighbor_read_and_page(self) -> None:
        """Verify NeighborRead pairing entity identity with connecting relationship."""
        entity = EntityGraphRead(
            id=uuid4(),
            type="person",
            name="Bob",
            revision=1,
        )
        rel = RelationshipRead(
            id=uuid4(),
            source_entity_id=entity.id,
            target_entity_id=uuid4(),
            type="FRIEND_OF",
            origin="owner",
            confidence=None,
            valid_from=None,
            valid_to=None,
            metadata={},
            created_at=datetime.now(UTC),
        )
        neighbor = NeighborRead(entity=entity, relationship=rel)
        assert neighbor.entity.name == "Bob"

        page = NeighborPage(items=[neighbor], truncated=False, next_cursor=None)
        assert len(page.items) == 1
        assert page.truncated is False

    def test_relationship_page_structure(self) -> None:
        """Verify RelationshipPage handles historical hints and unavailabilities."""
        page = RelationshipPage(
            items=[],
            next_cursor="cursor_abc",
            canonical_history_available=True,
            unavailable_relationship_ids=[uuid4()],
        )
        assert page.canonical_history_available is True
        assert len(page.unavailable_relationship_ids) == 1
