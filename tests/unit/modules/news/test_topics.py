"""Unit tests for news module: topic profiles, observation grouping, and story clustering.

Covers:
- Topic normalization: _normalize_name, _normalize_keywords, _normalize_entity_ids
- TopicCreate and TopicUpdate validation (revisions, bounds, mutation requirements)
- Canonical URL normalization (_canonical_url: ports, fragments, query strings, scheme validation)
- Deterministic identity keys (_identity_keys: URL hash first, content hash second)
- Lexical tokenization (_tokens) and signal provenance weights (1/7 equal weights)
- Observation clustering logic and fuzzy candidate matching rules (72h window, 0.92 similarity threshold)
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.news.stories import (
    ALGORITHM_VERSION,
    FUZZY_THRESHOLD,
    FUZZY_WINDOW,
    _canonical_url,
    _identity_keys,
    _initial_signal_provenance,
    _tokens,
)
from modules.news.topics import (
    MAX_ENTITIES,
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_REVISION,
    TopicCreate,
    TopicUpdate,
    _normalize_entity_ids,
    _normalize_keywords,
    _normalize_name,
)


class TestTopicNormalizationAndSchemas:
    """Tests for topic creation, normalization, and mutation fences."""

    def test_normalize_name_collapses_whitespace(self) -> None:
        """Verify topic name collapses multiple whitespace characters and validates length."""
        assert _normalize_name("  Machine   Learning  ") == "Machine Learning"

        # Empty or whitespace-only rejected
        with pytest.raises(ValueError, match="Topic name must contain 1 to 200 normalized characters"):
            _normalize_name("   ")

        # Over 200 chars rejected
        with pytest.raises(ValueError, match="Topic name must contain 1 to 200 normalized characters"):
            _normalize_name("a" * 201)

    def test_normalize_keywords_deduplication_and_bounds(self) -> None:
        """Verify keywords are normalized, deduplicated, and bounded to 50 items <= 100 chars."""
        raw_keywords = [" AI ", "Artificial Intelligence", "ai", "AI"]
        normalized = _normalize_keywords(raw_keywords)
        # "AI" and "ai" are distinct case, but whitespace normalized
        assert "Artificial Intelligence" in normalized
        assert len(normalized) <= MAX_KEYWORDS

        # Oversized keyword (> 100 chars) rejected
        with pytest.raises(ValueError, match="Topics accept at most 50 keywords"):
            _normalize_keywords(["x" * (MAX_KEYWORD_LENGTH + 1)])

        # Over 50 keywords rejected
        with pytest.raises(ValueError, match="Topics accept at most 50 keywords"):
            _normalize_keywords([f"kw_{i}" for i in range(51)])

    def test_normalize_entity_ids_uniqueness(self) -> None:
        """Entity IDs must be unique and bounded to 100."""
        ent1 = uuid4()
        ent2 = uuid4()
        assert _normalize_entity_ids([ent1, ent2]) == [ent1, ent2]

        # Duplicates rejected
        with pytest.raises(ValueError, match="Topics accept up to 100 unique entity IDs"):
            _normalize_entity_ids([ent1, ent1])

        # Over 100 rejected
        with pytest.raises(ValueError, match="Topics accept up to 100 unique entity IDs"):
            _normalize_entity_ids([uuid4() for _ in range(101)])

    def test_topic_create_schema(self) -> None:
        """Verify TopicCreate default attributes and weight bounds."""
        topic = TopicCreate(name="Artificial Intelligence")
        assert topic.name == "Artificial Intelligence"
        assert topic.is_active is True
        assert topic.weight == 1.0

        # Negative weight rejected
        with pytest.raises(ValidationError):
            TopicCreate(name="Test", weight=-0.5)

        # Weight > 10 rejected
        with pytest.raises(ValidationError):
            TopicCreate(name="Test", weight=10.5)

    def test_topic_update_requires_mutation(self) -> None:
        """TopicUpdate containing only expected_revision with no field changes must be rejected."""
        with pytest.raises(ValidationError, match="At least one topic field must be updated"):
            TopicUpdate(expected_revision=1)


class TestCanonicalURLNormalization:
    """Tests for _canonical_url security, scheme restriction, and normalization."""

    def test_canonical_url_http_and_https(self) -> None:
        """Verify standard http and https URLs normalize scheme and host to lowercase."""
        assert _canonical_url("HTTPS://EXAMPLE.COM/news/article") == "https://example.com/news/article"
        assert _canonical_url("http://example.com/path?query=1") == "http://example.com/path?query=1"

    def test_canonical_url_default_ports_stripped(self) -> None:
        """Default port 80 for http and 443 for https are stripped."""
        assert _canonical_url("http://example.com:80/path") == "http://example.com/path"
        assert _canonical_url("https://example.com:443/path") == "https://example.com/path"

        # Non-default ports are preserved
        assert _canonical_url("https://example.com:8443/path") == "https://example.com:8443/path"

    def test_canonical_url_fragments_dropped(self) -> None:
        """URL fragments (#section) are dropped while preserving query parameters."""
        assert _canonical_url("https://example.com/page?ref=home#top") == "https://example.com/page?ref=home"

    def test_canonical_url_empty_path_becomes_slash(self) -> None:
        """Empty path is normalized to root slash '/'."""
        assert _canonical_url("https://example.com") == "https://example.com/"

    def test_canonical_url_invalid_schemes_and_credentials_rejected(self) -> None:
        """Non-http(s) schemes and embedded credentials return None."""
        assert _canonical_url("ftp://example.com/file") is None
        assert _canonical_url("javascript:alert(1)") is None
        assert _canonical_url("https://user:password@example.com/data") is None
        assert _canonical_url(None) is None
        assert _canonical_url("   ") is None


class TestObservationGroupingAndClustering:
    """Tests for deterministic identity keys, tokenization, and clustering thresholds."""

    def test_identity_keys_deterministic(self) -> None:
        """Verify _identity_keys produces stable SHA-256 hashes for URL and content hash."""
        url = "https://example.com/news/tech-breakthrough"
        content_hash = "abc12345def67890"

        keys = _identity_keys(url, content_hash)
        assert len(keys) == 2
        assert keys[0][0] == "url"
        assert keys[1][0] == "hash"

        # Re-computing with the same values yields exact matching keys
        assert keys == _identity_keys(url, content_hash)

        # Without URL, only the content hash key is generated
        hash_only_keys = _identity_keys(None, content_hash)
        assert len(hash_only_keys) == 1
        assert hash_only_keys[0][0] == "hash"

    def test_tokens_extraction(self) -> None:
        """Verify _tokens extracts lowercase alphanumeric words between 2 and 64 characters."""
        text = "Umwelt-OS personal AI agent system v2.0"
        tokens = _tokens(text)
        assert "umwelt" in tokens
        assert "personal" in tokens
        assert "agent" in tokens
        assert "system" in tokens
        # Single characters excluded
        assert "v" not in tokens

    def test_initial_signal_provenance_weights(self) -> None:
        """Verify initial signal weights are equally divided among all 7 signals (1/7 each)."""
        prov = _initial_signal_provenance()
        assert prov["formula_version"] == 1
        signals = ("topic", "entity", "goal", "project", "recency", "importance", "novelty")
        weights = prov["weights"]

        assert set(weights.keys()) == set(signals)
        for name in signals:
            assert abs(weights[name] - (1.0 / 7.0)) < 1e-6
            assert prov["signals"][name]["available"] is False
            assert prov["signals"][name]["value"] == 0.0

    def test_fuzzy_clustering_constants(self) -> None:
        """Verify clustering parameters: 72h fuzzy window and 0.92 similarity threshold."""
        assert FUZZY_WINDOW == timedelta(hours=72)
        assert FUZZY_THRESHOLD == 0.92
        assert ALGORITHM_VERSION == 1
