"""Unit tests for entity schemas, types, aliases, bounds, and metadata validation.

Covers:
- EntityCreate, EntityPatch, AliasCreate, EntityRead, EntityAliasRead, and EntityPage.
- EntityType validation across all 15 supported literal types and rejection of invalid types.
- Alias normalization, trimming, uniqueness under canonicalization, and bounds.
- Name normalization and blank rejection.
- Metadata 64 KiB serialization and finiteness constraints.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.knowledge.entities.schemas import (
    AliasCreate,
    EntityAliasRead,
    EntityCreate,
    EntityPage,
    EntityPatch,
    EntityRead,
    canonicalize_name,
    validate_metadata,
)

VALID_ENTITY_TYPES: list[str] = [
    "person", "organization", "company", "project", "repository",
    "place", "country", "product", "topic", "technology",
    "asset", "device", "website", "event_subject", "other",
]


class TestCanonicalizeNameAndMetadata:
    """Tests for canonicalize_name and validate_metadata utility functions."""

    def test_canonicalize_name_normalizes_whitespace_and_case(self) -> None:
        """Verify canonicalize_name collapses internal/surrounding whitespace and casefolds."""
        assert canonicalize_name("  John   DOE  ") == "john doe"
        assert canonicalize_name("OpenAI,  Inc.") == "openai, inc."
        assert canonicalize_name("MÜNCHEN") == "münchen"

    def test_validate_metadata_valid_dict(self) -> None:
        """Verify validate_metadata accepts valid small json dicts."""
        data = {"key": "value", "numbers": [1, 2, 3], "nested": {"flag": True}}
        assert validate_metadata(data) == data

    def test_validate_metadata_rejects_exceeding_64kib(self) -> None:
        """Verify validate_metadata raises ValueError when compact json exceeds 65536 bytes."""
        large_data = {"key": "x" * 65530}
        with pytest.raises(ValueError, match="metadata exceeds 64 KiB"):
            validate_metadata(large_data)

    def test_validate_metadata_rejects_nan_values(self) -> None:
        """Verify validate_metadata rejects float NaN and Infinity."""
        with pytest.raises(ValueError):
            validate_metadata({"bad": float("nan")})

        with pytest.raises(ValueError):
            validate_metadata({"bad": float("inf")})


class TestEntityCreate:
    """Tests for EntityCreate schema validation, entity types, aliases, and bounds."""

    def test_valid_entity_create_all_types(self) -> None:
        """Verify EntityCreate accepts each of the 15 supported EntityType literals."""
        for etype in VALID_ENTITY_TYPES:
            entity = EntityCreate(
                type=etype,  # type: ignore[arg-type]
                name="Test Entity",
                description="A test entity description",
            )
            assert entity.type == etype
            assert entity.name == "Test Entity"
            assert entity.reason == "owner_create"

    def test_invalid_entity_type_rejected(self) -> None:
        """Verify unsupported entity types are rejected by validation."""
        with pytest.raises(ValidationError):
            EntityCreate(type="invalid_type", name="Test")  # type: ignore[arg-type]

    def test_name_trimming_and_blank_rejection(self) -> None:
        """Verify name whitespace is collapsed and blank-only strings are rejected."""
        entity = EntityCreate(type="person", name="   Alan   Turing   ")
        assert entity.name == "Alan Turing"

        with pytest.raises(ValidationError, match="name cannot be blank"):
            EntityCreate(type="person", name="     ")

        with pytest.raises(ValidationError):
            EntityCreate(type="person", name="")

    def test_name_length_bounds(self) -> None:
        """Verify name is bounded between 1 and 300 characters."""
        EntityCreate(type="project", name="a" * 300)

        with pytest.raises(ValidationError):
            EntityCreate(type="project", name="a" * 301)

    def test_description_length_bounds(self) -> None:
        """Verify description is bounded at 20000 characters."""
        entity = EntityCreate(type="place", name="Paris", description="p" * 20000)
        assert len(entity.description or "") == 20000

        with pytest.raises(ValidationError):
            EntityCreate(type="place", name="Paris", description="p" * 20001)

    def test_aliases_validation_and_uniqueness(self) -> None:
        """Verify aliases are trimmed, bounded, and unique under canonicalization."""
        entity = EntityCreate(
            type="company",
            name="Google",
            aliases=[" Alphabet ", "Google Inc "],
        )
        assert entity.aliases == ["Alphabet", "Google Inc"]

        # Rejects case-insensitive / canonical duplicates in aliases
        with pytest.raises(ValidationError, match="aliases must be unique"):
            EntityCreate(
                type="company",
                name="Google",
                aliases=["Alphabet", "  alphabet  "],
            )

        # Rejects empty alias strings
        with pytest.raises(ValidationError, match="aliases must contain 1 to 300 characters"):
            EntityCreate(
                type="company",
                name="Google",
                aliases=["   "],
            )

    def test_aliases_max_length_bound(self) -> None:
        """Verify at most 100 aliases are accepted."""
        valid_aliases = [f"Alias_{i}" for i in range(100)]
        entity = EntityCreate(type="topic", name="AI", aliases=valid_aliases)
        assert len(entity.aliases) == 100

        with pytest.raises(ValidationError):
            EntityCreate(type="topic", name="AI", aliases=valid_aliases + ["Extra"])

    def test_extra_fields_forbidden(self) -> None:
        """Verify extra unexpected fields are rejected on EntityCreate."""
        with pytest.raises(ValidationError):
            EntityCreate(type="person", name="Alice", extra_field=123)  # type: ignore[call-arg]


class TestEntityPatch:
    """Tests for EntityPatch revision fencing, partial updates, and bounds."""

    def test_valid_patch_minimal(self) -> None:
        """Verify EntityPatch requires expected_revision and reason."""
        patch = EntityPatch(expected_revision=1, reason="updating title")
        assert patch.expected_revision == 1
        assert patch.reason == "updating title"
        assert patch.name is None
        assert patch.description is None
        assert patch.metadata is None

    def test_expected_revision_must_be_ge_1(self) -> None:
        """Verify expected_revision rejects 0 or negative numbers."""
        with pytest.raises(ValidationError):
            EntityPatch(expected_revision=0, reason="test")

        with pytest.raises(ValidationError):
            EntityPatch(expected_revision=-2, reason="test")

    def test_patch_name_trimmed_and_cannot_be_blank(self) -> None:
        """Verify patch name collapses whitespace and rejects blank when supplied."""
        patch = EntityPatch(expected_revision=2, name="  New   Name  ")
        assert patch.name == "New Name"

        with pytest.raises(ValidationError, match="name cannot be blank"):
            EntityPatch(expected_revision=2, name="   ")

    def test_patch_extra_fields_forbidden(self) -> None:
        """Verify EntityPatch rejects extra fields."""
        with pytest.raises(ValidationError):
            EntityPatch(expected_revision=1, unexpected="val")  # type: ignore[call-arg]


class TestAliasCreate:
    """Tests for AliasCreate schema validation."""

    def test_valid_alias_create(self) -> None:
        """Verify AliasCreate trims alias and sets defaults."""
        alias = AliasCreate(alias="  G-Suite  ")
        assert alias.alias == "G-Suite"
        assert alias.confirmed is True
        assert alias.reason == "owner_alias"

    def test_blank_alias_rejected(self) -> None:
        """Verify blank alias is rejected."""
        with pytest.raises(ValidationError, match="alias cannot be blank"):
            AliasCreate(alias="    ")


class TestEntityReadAndPage:
    """Tests for EntityRead, EntityAliasRead, and EntityPage serialization."""

    def test_entity_alias_read(self) -> None:
        """Verify EntityAliasRead structure."""
        alias_id = uuid4()
        entity_id = uuid4()
        now = datetime.now(UTC)

        read = EntityAliasRead(
            id=alias_id,
            entity_id=entity_id,
            alias="GOOG",
            source_id=None,
            confirmed=True,
            origin="owner",
            confidence=1.0,
            created_at=now,
        )
        assert read.alias == "GOOG"
        assert read.origin == "owner"

    def test_entity_read_composition(self) -> None:
        """Verify EntityRead serialized structure."""
        entity_id = uuid4()
        now = datetime.now(UTC)

        entity = EntityRead(
            id=entity_id,
            type="company",
            name="Google LLC",
            canonical_name="google llc",
            description="Tech company",
            metadata={"sector": "technology"},
            revision=3,
            name_origin="owner",
            description_origin="derived",
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
            aliases=[],
        )
        assert entity.id == entity_id
        assert entity.revision == 3
        assert entity.metadata["sector"] == "technology"

    def test_entity_page_pagination(self) -> None:
        """Verify EntityPage holds items and optional continuation cursor."""
        page = EntityPage(items=[], next_cursor="curs_123")
        assert page.items == []
        assert page.next_cursor == "curs_123"
