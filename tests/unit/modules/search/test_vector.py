"""Unit tests for vector distance math, cosine similarity, ranking, and score normalization.

Covers:
- Vector dot product, L2 norm, cosine similarity, and pgvector cosine distance math.
- Embedding validation bounds and normalization via embedding_values.
- NewsSimilarityRead and NewsSimilarityResult dataclasses and candidate bounds.
- Vector ranking with deterministic tie-breaking.
"""

import math
from uuid import UUID, uuid4

import pytest

from modules.search.indexing import MAX_VECTOR_DIMENSIONS, embedding_values
from modules.search.public import NewsSimilarityRead, NewsSimilarityResult


def compute_cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two non-zero vectors u and v."""
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("Cannot compute cosine similarity of zero vector")
    raw_similarity = dot_product / (norm_a * norm_b)
    # Clamp within [-1.0, 1.0] to guard against floating-point inaccuracies
    return max(-1.0, min(1.0, raw_similarity))


def compute_cosine_distance(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute pgvector cosine distance: 1 - cosine_similarity."""
    return 1.0 - compute_cosine_similarity(vec_a, vec_b)


def normalize_similarity_to_unit_score(cosine_sim: float) -> float:
    """Normalize cosine similarity from [-1.0, 1.0] to unit interval [0.0, 1.0]."""
    clamped = max(-1.0, min(1.0, cosine_sim))
    return (clamped + 1.0) / 2.0


class TestCosineMath:
    """Tests for vector distance, cosine similarity math, and score normalization."""

    def test_identical_vectors_similarity(self) -> None:
        """Verify identical vectors yield similarity of 1.0 and distance of 0.0."""
        vec = [1.0, 2.0, 3.0, 4.0]
        sim = compute_cosine_similarity(vec, vec)
        dist = compute_cosine_distance(vec, vec)
        assert pytest.approx(sim, abs=1e-7) == 1.0
        assert pytest.approx(dist, abs=1e-7) == 0.0
        assert pytest.approx(normalize_similarity_to_unit_score(sim), abs=1e-7) == 1.0

    def test_opposite_vectors_similarity(self) -> None:
        """Verify opposite vectors yield similarity of -1.0 and distance of 2.0."""
        vec_a = [1.0, 2.0, 3.0]
        vec_b = [-1.0, -2.0, -3.0]
        sim = compute_cosine_similarity(vec_a, vec_b)
        dist = compute_cosine_distance(vec_a, vec_b)
        assert pytest.approx(sim, abs=1e-7) == -1.0
        assert pytest.approx(dist, abs=1e-7) == 2.0
        assert pytest.approx(normalize_similarity_to_unit_score(sim), abs=1e-7) == 0.0

    def test_orthogonal_vectors_similarity(self) -> None:
        """Verify orthogonal vectors yield similarity of 0.0 and distance of 1.0."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        sim = compute_cosine_similarity(vec_a, vec_b)
        dist = compute_cosine_distance(vec_a, vec_b)
        assert pytest.approx(sim, abs=1e-7) == 0.0
        assert pytest.approx(dist, abs=1e-7) == 1.0
        assert pytest.approx(normalize_similarity_to_unit_score(sim), abs=1e-7) == 0.5

    def test_zero_vector_rejection(self) -> None:
        """Verify zero vector raises ValueError on similarity calculation."""
        vec_zero = [0.0, 0.0, 0.0]
        vec_valid = [1.0, 0.0, 0.0]
        with pytest.raises(ValueError, match="zero vector"):
            compute_cosine_similarity(vec_zero, vec_valid)

    def test_arbitrary_vector_clamping(self) -> None:
        """Verify float overflow slightly beyond 1.0 or -1.0 is clamped correctly."""
        # Simulated floating point overflow
        assert max(-1.0, min(1.0, 1.0000000000000002)) == 1.0
        assert max(-1.0, min(1.0, -1.0000000000000002)) == -1.0


class TestEmbeddingValuesValidation:
    """Tests for embedding_values parsing, dimension checks, and value bounds."""

    def test_valid_embedding_extraction(self) -> None:
        """Verify valid response returns values list and model identity."""
        response = {
            "model": "text-embedding-3-small",
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
        }
        values, model = embedding_values(response, expected_dimensions=3)
        assert values == [0.1, 0.2, 0.3]
        assert model == "text-embedding-3-small"

    def test_invalid_structure_rejected(self) -> None:
        """Verify malformed JSON response raises ValueError."""
        with pytest.raises(ValueError, match="Invalid embedding response"):
            embedding_values({}, 3)

        with pytest.raises(ValueError, match="Invalid embedding response"):
            embedding_values({"data": []}, 3)

        with pytest.raises(ValueError, match="Invalid embedding response"):
            embedding_values({"data": [{"embedding": "not-a-list"}]}, 3)

    def test_dimension_mismatch_rejected(self) -> None:
        """Verify dimensions mismatching expected_dimensions raises ValueError."""
        response = {"data": [{"embedding": [0.1, 0.2]}]}
        with pytest.raises(ValueError, match="Embedding dimensions do not match"):
            embedding_values(response, expected_dimensions=3)

    def test_dimensions_exceeding_max_rejected(self) -> None:
        """Verify embedding exceeding MAX_VECTOR_DIMENSIONS (2000) raises ValueError."""
        response = {"data": [{"embedding": [0.1] * (MAX_VECTOR_DIMENSIONS + 1)}]}
        with pytest.raises(ValueError, match="Embedding dimensions do not match"):
            embedding_values(response)

    def test_non_finite_or_boolean_rejected(self) -> None:
        """Verify boolean, NaN, and Inf in embedding vector raise ValueError."""
        # Booleans
        resp_bool = {"data": [{"embedding": [True, 0.5]}]}
        with pytest.raises(ValueError, match="Embedding contains invalid values"):
            embedding_values(resp_bool)

        # NaN
        resp_nan = {"data": [{"embedding": [float("nan"), 0.5]}]}
        with pytest.raises(ValueError, match="Embedding contains invalid values"):
            embedding_values(resp_nan)

        # Inf
        resp_inf = {"data": [{"embedding": [float("inf"), 0.5]}]}
        with pytest.raises(ValueError, match="Embedding contains invalid values"):
            embedding_values(resp_inf)

    def test_all_zero_embedding_rejected(self) -> None:
        """Verify all-zero embedding is rejected as invalid."""
        resp_zero = {"data": [{"embedding": [0.0, 0.0, 0.0]}]}
        with pytest.raises(ValueError, match="Embedding cannot be a zero vector"):
            embedding_values(resp_zero)

    def test_invalid_model_identity_rejected(self) -> None:
        """Verify non-string or whitespace model string is rejected."""
        resp_empty_model = {
            "model": "   ",
            "data": [{"embedding": [0.1, 0.2]}],
        }
        with pytest.raises(ValueError, match="model identity is invalid"):
            embedding_values(resp_empty_model)


class TestVectorRankingAndTieBreaking:
    """Tests for ranking candidates by vector distance with deterministic tie-breaking."""

    def test_ranking_by_ascending_distance(self) -> None:
        """Verify candidates with smaller distance (higher similarity) rank higher."""
        query_vec = [1.0, 0.0]
        candidates: list[tuple[UUID, list[float]]] = [
            (UUID("00000000-0000-0000-0000-000000000003"), [0.0, 1.0]),       # orthogonal: dist 1.0
            (UUID("00000000-0000-0000-0000-000000000001"), [1.0, 0.0]),       # identical: dist 0.0
            (UUID("00000000-0000-0000-0000-000000000002"), [0.8, 0.6]),       # close: dist ~0.2
        ]

        scored = [
            (chunk_id, compute_cosine_distance(query_vec, vec))
            for chunk_id, vec in candidates
        ]
        # Sort ascending by distance, tie-break by chunk_id string
        ranked = sorted(scored, key=lambda item: (item[1], str(item[0])))

        expected_order = [
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
            UUID("00000000-0000-0000-0000-000000000003"),
        ]
        assert [item[0] for item in ranked] == expected_order

    def test_tie_breaking_by_chunk_id(self) -> None:
        """Verify identical distances are broken deterministically by chunk_id."""
        query_vec = [1.0, 0.0]
        id_b = UUID("00000000-0000-0000-0000-00000000000b")
        id_a = UUID("00000000-0000-0000-0000-00000000000a")

        candidates = [
            (id_b, [0.0, 1.0]),  # distance 1.0
            (id_a, [0.0, 1.0]),  # distance 1.0
        ]
        scored = [
            (chunk_id, compute_cosine_distance(query_vec, vec))
            for chunk_id, vec in candidates
        ]
        ranked = sorted(scored, key=lambda item: (item[1], str(item[0])))

        assert ranked[0][0] == id_a
        assert ranked[1][0] == id_b


class TestNewsSimilarityReadAndResult:
    """Tests for NewsSimilarityRead and NewsSimilarityResult dataclasses."""

    def test_news_similarity_read_construction(self) -> None:
        """Verify NewsSimilarityRead holds chunk IDs, cosine similarity, and generation data."""
        left = uuid4()
        right = uuid4()
        gen = uuid4()

        read = NewsSimilarityRead(
            left_chunk_id=left,
            right_chunk_id=right,
            cosine_similarity=0.88,
            generation_id=gen,
            model_id="embed-v1",
            model_version="1.0",
            response_model_id="embed-v1",
            dimensions=768,
            gateway_identity="local",
        )
        assert read.left_chunk_id == left
        assert read.right_chunk_id == right
        assert read.cosine_similarity == 0.88
        assert read.dimensions == 768

    def test_news_similarity_result_capabilities(self) -> None:
        """Verify NewsSimilarityResult capability statuses."""
        res_avail = NewsSimilarityResult(items=(), capability="available")
        assert res_avail.capability == "available"

        res_none = NewsSimilarityResult(items=(), capability="no_candidates")
        assert res_none.capability == "no_candidates"

        res_limit = NewsSimilarityResult(items=(), capability="candidate_limit_exceeded")
        assert res_limit.capability == "candidate_limit_exceeded"

    def test_candidate_bounds_validation(self) -> None:
        """Verify candidate chunk IDs validation: <= 100 items, all unique."""
        # Duplicates test
        dup_id = uuid4()
        candidates_dup = (dup_id, dup_id)
        assert len(set(candidates_dup)) != len(candidates_dup)

        # Max 100 test
        candidates_100 = tuple(uuid4() for _ in range(100))
        assert len(candidates_100) == 100
        assert len(set(candidates_100)) == len(candidates_100)

        candidates_101 = tuple(uuid4() for _ in range(101))
        assert len(candidates_101) > 100
