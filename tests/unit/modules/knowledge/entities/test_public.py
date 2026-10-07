"""Unit tests for entity deduplication logic, extraction parsing, and normalizations.

Covers:
- resolve_candidate matching against confirmed aliases and fuzzy name similarity.
- candidate_match_fingerprint hashing for deduplication and review tracking.
- ExtractedEntity, ExtractedRelationship, and ExtractionOutput bounds and validations.
- response_content parsing of model choices, content bounds, and usage sanitation.
- extraction_messages prompt formatting and untrusted chunk wrappers.
- response_schema strict JSON schema generation.
"""

import json
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.knowledge.entities.extraction import (
    MAX_ENTITY_CHUNKS,
    MAX_FACTS,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionOutput,
    extraction_messages,
    response_content,
    response_schema,
)
from modules.knowledge.entities.resolution import (
    candidate_match_fingerprint,
    resolve_candidate,
)


class TestEntityResolutionAndDeduplication:
    """Tests for resolve_candidate and candidate_match_fingerprint."""

    def test_candidate_match_fingerprint_invariance(self) -> None:
        """Verify fingerprint is case and whitespace invariant."""
        fp1 = candidate_match_fingerprint("  Apple  Inc  ", "company")
        fp2 = candidate_match_fingerprint("apple inc", "company")
        assert fp1 == fp2
        assert fp1 == sha256(b"company:apple inc").hexdigest()

    def test_candidate_match_fingerprint_differs_by_type(self) -> None:
        """Verify fingerprint differs when entity types differ."""
        fp_company = candidate_match_fingerprint("Tesla", "company")
        fp_person = candidate_match_fingerprint("Tesla", "person")
        assert fp_company != fp_person

    def test_resolve_candidate_single_confirmed_alias_match(self) -> None:
        """Verify exactly one confirmed alias match returns status 'matched' with target ID."""
        target_id = str(uuid4())
        known = [
            {
                "id": target_id,
                "type": "company",
                "name": "Alphabet Inc.",
                "confirmed_aliases": ["google", "goog"],
            },
            {
                "id": str(uuid4()),
                "type": "company",
                "name": "Microsoft Corp",
                "confirmed_aliases": ["msft"],
            },
        ]
        status, matched_id, candidates = resolve_candidate("Google", "company", known)
        assert status == "matched"
        assert matched_id == target_id
        assert candidates == []

    def test_resolve_candidate_multiple_alias_matches_triggers_review(self) -> None:
        """Verify multiple confirmed alias matches across entities triggers 'review'."""
        id1 = str(uuid4())
        id2 = str(uuid4())
        known = [
            {
                "id": id1,
                "type": "technology",
                "name": "Python Language",
                "confirmed_aliases": ["python"],
            },
            {
                "id": id2,
                "type": "technology",
                "name": "Python Monty",
                "confirmed_aliases": ["python"],
            },
        ]
        status, matched_id, candidates = resolve_candidate("python", "technology", known)
        assert status == "review"
        assert matched_id is None
        assert sorted(candidates) == sorted([id1, id2])

    def test_resolve_candidate_fuzzy_name_similarity_triggers_review(self) -> None:
        """Verify high name similarity (ratio >= 0.78) without alias match triggers 'review'."""
        id1 = str(uuid4())
        known = [
            {
                "id": id1,
                "type": "person",
                "name": "Alexander Graham Bell",
                "confirmed_aliases": [],
            }
        ]
        # "Alexander Graham Bell" vs "Alexander G. Bell" has ratio ~0.84
        status, matched_id, candidates = resolve_candidate("Alexander G. Bell", "person", known)
        assert status == "review"
        assert matched_id is None
        assert candidates == [id1]

    def test_resolve_candidate_different_type_ignored(self) -> None:
        """Verify candidates of differing entity types are completely ignored."""
        known = [
            {
                "id": str(uuid4()),
                "type": "organization",
                "name": "Linux",
                "confirmed_aliases": ["linux"],
            }
        ]
        # Querying for technology type "Linux" should not match organization "Linux"
        status, matched_id, candidates = resolve_candidate("Linux", "technology", known)
        assert status == "new"
        assert matched_id is None
        assert candidates == []

    def test_resolve_candidate_new_entity(self) -> None:
        """Verify unrelated entity returns 'new' with empty candidates."""
        known = [
            {
                "id": str(uuid4()),
                "type": "company",
                "name": "Stripe",
                "confirmed_aliases": [],
            }
        ]
        status, matched_id, candidates = resolve_candidate("Acme Corp", "company", known)
        assert status == "new"
        assert matched_id is None
        assert candidates == []


class TestExtractionModels:
    """Tests for ExtractedEntity, ExtractedRelationship, and ExtractionOutput schemas."""

    def test_extracted_entity_validation(self) -> None:
        """Verify ExtractedEntity normalizes key, name, trims description, and bounds chunks."""
        chunk_id = uuid4()
        entity = ExtractedEntity(
            key="  k1  ",
            name="  OpenAI  ",
            type="organization",
            description="  AI research organization.  ",
            chunk_ids=[chunk_id],
            confidence=0.95,
        )
        assert entity.key == "k1"
        assert entity.name == "OpenAI"
        assert entity.description == "AI research organization."
        assert entity.confidence == 0.95

    def test_extracted_entity_empty_description_becomes_none(self) -> None:
        """Verify blank or whitespace description is cleaned to None."""
        entity = ExtractedEntity(
            key="k1",
            name="Name",
            type="person",
            description="   ",
            chunk_ids=[uuid4()],
            confidence=0.9,
        )
        assert entity.description is None

    def test_extracted_entity_rejects_duplicate_chunks(self) -> None:
        """Verify duplicate chunk IDs within one entity are rejected."""
        cid = uuid4()
        with pytest.raises(ValidationError, match="chunk IDs must be unique"):
            ExtractedEntity(
                key="k1",
                name="Test",
                type="topic",
                description=None,
                chunk_ids=[cid, cid],
                confidence=0.8,
            )

    def test_extracted_entity_chunks_bound(self) -> None:
        """Verify chunk citations are capped at MAX_ENTITY_CHUNKS (5)."""
        valid_chunks = [uuid4() for _ in range(MAX_ENTITY_CHUNKS)]
        entity = ExtractedEntity(
            key="k1",
            name="Test",
            type="topic",
            description=None,
            chunk_ids=valid_chunks,
            confidence=0.8,
        )
        assert len(entity.chunk_ids) == MAX_ENTITY_CHUNKS

        with pytest.raises(ValidationError):
            ExtractedEntity(
                key="k1",
                name="Test",
                type="topic",
                description=None,
                chunk_ids=valid_chunks + [uuid4()],
                confidence=0.8,
            )

    def test_extracted_relationship_type_pattern(self) -> None:
        """Verify ExtractedRelationship type enforces ^[A-Z][A-Z0-9_]*$ pattern."""
        valid_rel = ExtractedRelationship(
            source_key="k1",
            target_key="k2",
            type="FOUNDED_BY",
            chunk_id=uuid4(),
            confidence=0.88,
        )
        assert valid_rel.type == "FOUNDED_BY"

        with pytest.raises(ValidationError):
            ExtractedRelationship(
                source_key="k1",
                target_key="k2",
                type="founded_by",  # lowercase invalid
                chunk_id=uuid4(),
                confidence=0.88,
            )

    def test_confidence_validation(self) -> None:
        """Verify confidence rejects booleans, NaN, and values outside [0, 1]."""
        with pytest.raises(ValidationError, match="confidence must be a JSON number"):
            ExtractedRelationship(
                source_key="k1",
                target_key="k2",
                type="WORKS_AT",
                chunk_id=uuid4(),
                confidence=True,  # type: ignore[arg-type]
            )

        with pytest.raises(ValidationError, match="confidence must be finite and in"):
            ExtractedRelationship(
                source_key="k1",
                target_key="k2",
                type="WORKS_AT",
                chunk_id=uuid4(),
                confidence=1.5,
            )

    def test_extraction_output_max_facts_bound(self) -> None:
        """Verify ExtractionOutput enforces MAX_FACTS (30) on entities and relationships."""
        chunk_id = uuid4()
        entities = [
            ExtractedEntity(
                key=f"k{i}",
                name=f"Entity {i}",
                type="other",
                description=None,
                chunk_ids=[chunk_id],
                confidence=0.8,
            )
            for i in range(MAX_FACTS)
        ]
        output = ExtractionOutput(entities=entities, relationships=[])
        assert len(output.entities) == MAX_FACTS

        with pytest.raises(ValidationError):
            extra = ExtractedEntity(
                key="extra",
                name="Extra",
                type="other",
                description=None,
                chunk_ids=[chunk_id],
                confidence=0.8,
            )
            ExtractionOutput(entities=entities + [extra], relationships=[])


class TestExtractionResponseParsing:
    """Tests for response_content, response_schema, and extraction_messages."""

    def test_response_content_valid_payload(self) -> None:
        """Verify valid model response is parsed into ExtractionOutput."""
        cid = str(uuid4())
        content_json = {
            "entities": [
                {
                    "key": "k1",
                    "name": "DeepMind",
                    "type": "company",
                    "description": "AI lab",
                    "chunk_ids": [cid],
                    "confidence": 0.99,
                }
            ],
            "relationships": [],
        }
        response = {
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": json.dumps(content_json)}}],
            "usage": {"total_tokens": 150},
        }

        output, model, usage = response_content(response)
        assert len(output.entities) == 1
        assert output.entities[0].name == "DeepMind"
        assert model == "gpt-4o-mini"
        assert usage == {"total_tokens": 150}

    def test_response_content_malformed_rejected(self) -> None:
        """Verify malformed model response raises ValueError('invalid_model_response')."""
        with pytest.raises(ValueError, match="invalid_model_response"):
            response_content({})

        with pytest.raises(ValueError, match="invalid_model_response"):
            response_content({"choices": []})

        with pytest.raises(ValueError, match="invalid_model_response"):
            response_content({"choices": [{"message": {"content": None}}]})

    def test_response_content_size_limit(self) -> None:
        """Verify response content exceeding 32000 bytes raises ValueError."""
        oversized = "x" * 32001
        response = {"choices": [{"message": {"content": oversized}}]}
        with pytest.raises(ValueError, match="invalid_model_response"):
            response_content(response)

    def test_response_schema_structure(self) -> None:
        """Verify response_schema returns strict JSON schema wrapper."""
        schema_dict = response_schema()
        assert schema_dict["name"] == "entity_extraction_v1"
        assert schema_dict["strict"] is True
        assert "schema" in schema_dict

    def test_extraction_messages_formatting(self) -> None:
        """Verify extraction_messages wraps chunk IDs and contents in XML tags."""
        c1 = uuid4()
        c2 = uuid4()
        chunks = [
            (c1, "Alice works at DeepMind."),
            (c2, "DeepMind is located in London."),
        ]
        messages = extraction_messages(chunks)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "untrusted data" in str(messages[0]["content"])

        user_content = str(messages[1]["content"])
        assert f'<chunk id="{c1}">Alice works at DeepMind.</chunk>' in user_content
        assert f'<chunk id="{c2}">DeepMind is located in London.</chunk>' in user_content
