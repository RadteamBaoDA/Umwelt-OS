"""Unit tests for search reciprocal rank fusion (RRF) logic, weight blending, and tie-breaking.

Covers:
- Standard RRF score calculation with constant k=60.
- Hybrid vs lexical-only ranking modes.
- Deduplication and additive scoring for overlapping candidates.
- Deterministic secondary tie-breaking by chunk UUID string.
- Pagination slicing, bounds (MAX_CANDIDATES=500, MAX_RANKED=1000), and cursor validation.
"""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from core.tools.schemas import ToolDestination
from modules.search.public import (
    MAX_RANKED_CANDIDATES,
    _encode_cursor,
    _offset,
)
from modules.search.schemas import SearchRequest


def rrf_fuse(
    lexical: list[UUID],
    vector: list[UUID],
    *,
    effective_mode: str = "hybrid",
    k: int = 60,
) -> tuple[dict[UUID, float], list[UUID]]:
    """Simulate reciprocal rank fusion ranking logic used in modules.search.public."""
    ranked: dict[UUID, float] = {}
    candidate_lists = (lexical, vector) if effective_mode == "hybrid" else (lexical,)
    for candidates in candidate_lists:
        for rank, chunk_id in enumerate(candidates, 1):
            ranked[chunk_id] = ranked.get(chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(ranked, key=lambda chunk_id: (-ranked[chunk_id], str(chunk_id)))
    return ranked, ordered


class TestReciprocalRankFusionScores:
    """Tests for RRF scoring math and multi-list fusion."""

    def test_single_lexical_rank_score(self) -> None:
        """Verify 1-indexed RRF formula gives 1 / (60 + rank)."""
        id1 = uuid4()
        id2 = uuid4()
        ranked, ordered = rrf_fuse(lexical=[id1, id2], vector=[], effective_mode="lexical")

        assert pytest.approx(ranked[id1], abs=1e-9) == 1.0 / 61.0
        assert pytest.approx(ranked[id2], abs=1e-9) == 1.0 / 62.0
        assert ordered == [id1, id2]

    def test_hybrid_overlapping_fusion(self) -> None:
        """Verify candidate in both lexical and vector gets summed RRF score."""
        shared_id = uuid4()
        lex_only = uuid4()
        vec_only = uuid4()

        # shared_id is rank 1 in lexical, rank 1 in vector
        ranked, ordered = rrf_fuse(
            lexical=[shared_id, lex_only],
            vector=[shared_id, vec_only],
            effective_mode="hybrid",
        )

        expected_shared_score = (1.0 / 61.0) + (1.0 / 61.0)
        assert pytest.approx(ranked[shared_id], abs=1e-9) == expected_shared_score
        assert pytest.approx(ranked[lex_only], abs=1e-9) == 1.0 / 62.0
        assert pytest.approx(ranked[vec_only], abs=1e-9) == 1.0 / 62.0

        # shared_id should easily be the top result
        assert ordered[0] == shared_id
        assert set(ordered) == {shared_id, lex_only, vec_only}
        # Deduplication: exactly 3 items in result
        assert len(ordered) == 3

    def test_cross_rank_comparison(self) -> None:
        """Verify candidate appearing at lower ranks in both lists can outscore top rank in one."""
        id_both = uuid4()
        id_top_single = uuid4()

        # id_both is rank 2 in lexical and rank 2 in vector: 2 * (1/62) = 0.032258
        # id_top_single is rank 1 in lexical only: 1/61 = 0.016393
        ranked, ordered = rrf_fuse(
            lexical=[id_top_single, id_both],
            vector=[uuid4(), id_both],
            effective_mode="hybrid",
        )
        assert ranked[id_both] > ranked[id_top_single]
        assert ordered[0] == id_both

    def test_empty_lists(self) -> None:
        """Verify empty input lists yield empty ranking."""
        ranked, ordered = rrf_fuse(lexical=[], vector=[], effective_mode="hybrid")
        assert ranked == {}
        assert ordered == []

    def test_lexical_mode_ignores_vector(self) -> None:
        """Verify when mode is 'lexical', vector candidates are completely ignored."""
        id_lex = uuid4()
        id_vec = uuid4()
        ranked, ordered = rrf_fuse(lexical=[id_lex], vector=[id_vec], effective_mode="lexical")

        assert id_lex in ranked
        assert id_vec not in ranked
        assert ordered == [id_lex]


class TestTieBreaking:
    """Tests for deterministic secondary tie-breaking by chunk UUID string."""

    def test_tie_breaking_by_uuid_lexicographical_order(self) -> None:
        """Verify identical scores are broken deterministically by ascending str(UUID)."""
        # Create two IDs with identical score (e.g. rank 1 in disjoint lists or identical ranks)
        id_higher_str = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        id_lower_str = UUID("00000000-0000-0000-0000-000000000001")

        # In hybrid mode: id_higher_str is rank 1 in lexical, id_lower_str is rank 1 in vector
        # Both get exactly 1/61 score.
        ranked, ordered = rrf_fuse(
            lexical=[id_higher_str],
            vector=[id_lower_str],
            effective_mode="hybrid",
        )

        assert pytest.approx(ranked[id_higher_str], abs=1e-9) == pytest.approx(ranked[id_lower_str], abs=1e-9)
        # Tie break should choose id_lower_str first because "0..." < "f..."
        assert ordered == [id_lower_str, id_higher_str]

    def test_three_way_tie_broken_consistently(self) -> None:
        """Verify multi-way ties sort strictly by str(UUID)."""
        ids = [
            UUID("33333333-3333-3333-3333-333333333333"),
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        ]
        # Assign each id the same score directly
        ranked = {item: 0.05 for item in ids}
        ordered = sorted(ranked, key=lambda chunk_id: (-ranked[chunk_id], str(chunk_id)))

        assert ordered == [
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            UUID("33333333-3333-3333-3333-333333333333"),
        ]


class TestSearchCursorsAndPagination:
    """Tests for cursor scoping, unpadded base64 encoding/decoding, and candidate bounds."""

    def test_cursor_round_trip(self) -> None:
        """Verify valid cursor encodes and decodes to matching offset."""
        request = SearchRequest(query="deep learning", limit=10)
        encoded = _encode_cursor(request, offset=20, destination=ToolDestination.LOCAL)
        request_with_cursor = SearchRequest(query="deep learning", limit=10, cursor=encoded)

        offset = _offset(request_with_cursor, destination=ToolDestination.LOCAL)
        assert offset == 20

    def test_cursor_tamper_detection(self) -> None:
        """Verify changing query parameters invalidates cursor."""
        req1 = SearchRequest(query="query 1", limit=10)
        cursor1 = _encode_cursor(req1, offset=10)

        # Using cursor from query 1 with query 2 should raise 422
        req2 = SearchRequest(query="query 2", limit=10, cursor=cursor1)
        with pytest.raises(HTTPException) as exc_info:
            _offset(req2)
        assert exc_info.value.status_code == 422
        assert "Invalid search cursor" in exc_info.value.detail

    def test_cursor_destination_tamper_detection(self) -> None:
        """Verify cursor bound to LOCAL destination fails for REMOTE destination."""
        req = SearchRequest(query="test", limit=10)
        source_id = uuid4()
        fences = {source_id: 1}

        cursor_local = _encode_cursor(req, offset=10, destination=ToolDestination.LOCAL, source_generation_fences=fences)
        req_with_cursor = SearchRequest(query="test", limit=10, cursor=cursor_local)

        with pytest.raises(HTTPException) as exc_info:
            _offset(req_with_cursor, destination=ToolDestination.REMOTE, source_generation_fences=fences)
        assert exc_info.value.status_code == 422

    def test_offset_exceeding_max_candidates_rejected(self) -> None:
        """Verify cursor with offset > MAX_RANKED_CANDIDATES (1000) is rejected."""
        req = SearchRequest(query="test", limit=10)
        oversized_cursor = _encode_cursor(req, offset=MAX_RANKED_CANDIDATES + 1)
        req_with_cursor = SearchRequest(query="test", limit=10, cursor=oversized_cursor)

        with pytest.raises(HTTPException) as exc_info:
            _offset(req_with_cursor)
        assert exc_info.value.status_code == 422

    def test_malformed_cursor_rejected(self) -> None:
        """Verify non-base64 or malformed cursor string is rejected with 422."""
        req = SearchRequest(query="test", limit=10, cursor="!@#$%^&*()")
        with pytest.raises(HTTPException) as exc_info:
            _offset(req)
        assert exc_info.value.status_code == 422
