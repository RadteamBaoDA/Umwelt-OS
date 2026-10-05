"""Unit tests for search module schemas, ranking options, and bounds.

Covers SearchRequest (SearchQuery), SearchFilters (SearchFilter), SearchHit/SearchResponse (SearchResult),
Citation, SearchSource, SearchIndexStatus, and GlobalSearchResponse.
"""

from datetime import UTC, datetime
from uuid import uuid4
import pytest
from pydantic import ValidationError

from modules.search.schemas import (
    Citation,
    GlobalSearchResponse,
    ReindexResponse,
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
    SearchRequest,
    SearchResponse,
    SearchSource,
)

# Semantic aliases per prompt specification
SearchQuery = SearchRequest
SearchFilter = SearchFilters
SearchResult = SearchHit


class TestSearchFilters:
    """Tests for SearchFilters bounds and schema validation."""

    def test_default_filters_are_empty(self) -> None:
        """Verify defaults for SearchFilters have empty lists and None dates."""
        filters = SearchFilters()
        assert filters.source_ids == []
        assert filters.date_from is None
        assert filters.date_to is None
        assert filters.content_types == []

    def test_valid_filters_with_values(self) -> None:
        """Verify SearchFilters accepts valid source UUIDs, dates, and content types."""
        source_id = uuid4()
        now = datetime.now(UTC)
        filters = SearchFilters(
            source_ids=[source_id],
            date_from=now,
            date_to=now,
            content_types=["text/plain", "application/pdf"],
        )
        assert filters.source_ids == [source_id]
        assert filters.date_from == now
        assert filters.date_to == now
        assert filters.content_types == ["text/plain", "application/pdf"]

    def test_forbid_extra_fields(self) -> None:
        """Verify SearchFilters forbids unrecognized extra fields."""
        with pytest.raises(ValidationError) as exc_info:
            SearchFilters(unknown_field="value")  # type: ignore[call-arg]
        assert "extra_forbidden" in str(exc_info.value)

    def test_source_ids_bounded_at_100(self) -> None:
        """Verify source_ids accepts up to 100 items but rejects 101."""
        valid_ids = [uuid4() for _ in range(100)]
        filters = SearchFilters(source_ids=valid_ids)
        assert len(filters.source_ids) == 100

        with pytest.raises(ValidationError) as exc_info:
            SearchFilters(source_ids=valid_ids + [uuid4()])
        assert "source_ids" in str(exc_info.value)

    def test_content_types_bounded_at_20(self) -> None:
        """Verify content_types accepts up to 20 items but rejects 21."""
        valid_types = [f"type_{i}" for i in range(20)]
        filters = SearchFilters(content_types=valid_types)
        assert len(filters.content_types) == 20

        with pytest.raises(ValidationError) as exc_info:
            SearchFilters(content_types=valid_types + ["type_extra"])
        assert "content_types" in str(exc_info.value)


class TestSearchRequest:
    """Tests for SearchRequest (SearchQuery) ranking options, parameters, and bounds."""

    def test_default_search_request(self) -> None:
        """Verify SearchRequest defaults: hybrid mode, limit 20, empty filters, no cursor."""
        req = SearchRequest(query="machine learning")
        assert req.query == "machine learning"
        assert req.mode == "hybrid"
        assert req.limit == 20
        assert req.cursor is None
        assert req.filters.source_ids == []

    def test_search_query_alias(self) -> None:
        """Verify SearchQuery alias behaves identically to SearchRequest."""
        query = SearchQuery(query="test query")
        assert isinstance(query, SearchRequest)
        assert query.query == "test query"

    def test_forbid_extra_fields(self) -> None:
        """Verify SearchRequest forbids unrecognized extra fields."""
        with pytest.raises(ValidationError):
            SearchRequest(query="valid", extra_param="forbidden")  # type: ignore[call-arg]

    def test_query_length_bounds(self) -> None:
        """Verify query string cannot be empty and is capped at 1000 characters."""
        with pytest.raises(ValidationError):
            SearchRequest(query="")

        # 1000 characters is allowed
        req_1000 = SearchRequest(query="a" * 1000)
        assert len(req_1000.query) == 1000

        # 1001 characters is rejected
        with pytest.raises(ValidationError):
            SearchRequest(query="a" * 1001)

    def test_mode_options(self) -> None:
        """Verify supported modes are 'lexical' and 'hybrid', other values rejected."""
        req_lex = SearchRequest(query="hello", mode="lexical")
        assert req_lex.mode == "lexical"

        req_hyb = SearchRequest(query="hello", mode="hybrid")
        assert req_hyb.mode == "hybrid"

        with pytest.raises(ValidationError):
            SearchRequest(query="hello", mode="semantic")  # type: ignore[arg-type]

    def test_limit_bounds(self) -> None:
        """Verify limit must be between 1 and 100 inclusive."""
        assert SearchRequest(query="hello", limit=1).limit == 1
        assert SearchRequest(query="hello", limit=100).limit == 100

        with pytest.raises(ValidationError):
            SearchRequest(query="hello", limit=0)

        with pytest.raises(ValidationError):
            SearchRequest(query="hello", limit=-5)

        with pytest.raises(ValidationError):
            SearchRequest(query="hello", limit=101)

    def test_cursor_bound(self) -> None:
        """Verify cursor length is bounded at 256 characters."""
        valid_cursor = "c" * 256
        req = SearchRequest(query="hello", cursor=valid_cursor)
        assert req.cursor == valid_cursor

        with pytest.raises(ValidationError):
            SearchRequest(query="hello", cursor="c" * 257)


class TestSearchHitAndCitation:
    """Tests for SearchHit, Citation, and SearchSource schemas."""

    def test_citation_creation(self) -> None:
        """Verify Citation holds document chunk provenance and required fields."""
        citation = Citation(
            sourceId=uuid4(),
            documentId=uuid4(),
            chunkId=uuid4(),
            title="Intro to AI",
            url="https://example.com/doc",
            observedAt=datetime.now(UTC),
            quote="AI is a field of computer science...",
        )
        assert citation.sourceType == "document"
        assert citation.title == "Intro to AI"
        assert citation.url == "https://example.com/doc"

    def test_search_source(self) -> None:
        """Verify SearchSource identity fields."""
        source_id = uuid4()
        source = SearchSource(id=source_id, name="GitHub Repo", type="github")
        assert source.id == source_id
        assert source.name == "GitHub Repo"
        assert source.type == "github"

    def test_search_hit_composition(self) -> None:
        """Verify SearchHit (SearchResult) serialization and nested objects."""
        doc_id = uuid4()
        version_id = uuid4()
        chunk_id = uuid4()
        source_id = uuid4()

        source = SearchSource(id=source_id, name="Local Notes", type="markdown")
        citation = Citation(
            sourceId=source_id,
            documentId=doc_id,
            chunkId=chunk_id,
            title="My Note",
            url=None,
            observedAt=None,
            quote="Excerpt content",
        )

        hit = SearchHit(
            document_id=doc_id,
            document_version_id=version_id,
            version_number=1,
            chunk_id=chunk_id,
            title="My Note",
            excerpt="Excerpt content",
            score=0.85,
            source=source,
            observed_at=None,
            published_at=None,
            content_type="text/markdown",
            entity_refs=[],
            citation=citation,
        )
        assert hit.score == 0.85
        assert hit.version_number == 1
        assert hit.citation.quote == "Excerpt content"
        assert hit.entity_refs == []


class TestSearchResponse:
    """Tests for SearchResponse and index status responses."""

    def test_search_response_structure(self) -> None:
        """Verify SearchResponse returns hits, next_cursor, effective_mode, and warnings."""
        resp = SearchResponse(
            items=[],
            next_cursor=None,
            effective_mode="hybrid",
            warnings=["Semantic search unavailable"],
        )
        assert resp.items == []
        assert resp.next_cursor is None
        assert resp.effective_mode == "hybrid"
        assert resp.warnings == ["Semantic search unavailable"]

    def test_invalid_effective_mode_rejected(self) -> None:
        """Verify effective_mode only allows 'lexical' or 'hybrid'."""
        with pytest.raises(ValidationError):
            SearchResponse(
                items=[],
                next_cursor=None,
                effective_mode="vector",  # type: ignore[arg-type]
                warnings=[],
            )

    def test_reindex_response(self) -> None:
        """Verify ReindexResponse wraps run_id."""
        run_id = uuid4()
        reindex = ReindexResponse(run_id=run_id)
        assert reindex.run_id == run_id

    def test_search_index_status(self) -> None:
        """Verify SearchIndexStatus fields."""
        status = SearchIndexStatus(
            run_id=uuid4(),
            status="active",
            model_id="text-embedding-3-small",
            dimensions=1536,
            indexed_items=42,
            failed_items=0,
        )
        assert status.status == "active"
        assert status.dimensions == 1536
        assert status.indexed_items == 42

    def test_global_search_response(self) -> None:
        """Verify GlobalSearchResponse initializes defaults for documents, tasks, and goals."""
        resp = GlobalSearchResponse()
        assert resp.documents == []
        assert resp.tasks == []
        assert resp.goals == []
        assert resp.document_next_cursor is None
        assert resp.task_next_cursor is None
        assert resp.goal_next_cursor is None
        assert resp.total == 0
