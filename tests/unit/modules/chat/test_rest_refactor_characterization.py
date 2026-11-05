"""Characterization tests for the helpers split out of long chat functions (citations, retrieval, db_query)."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.chat import citations, db_query, retrieval
from modules.chat.schemas import MAX_QUOTE_LENGTH, AnswerContextRequest, Citation, EvidenceItem


def _evidence(content: str = "alpha beta gamma delta") -> EvidenceItem:
    """Build a minimal valid evidence item with fresh identifiers."""
    return EvidenceItem(
        source_id=uuid4(), source_type="document", source_generation=1, local_only=False,
        document_id=uuid4(), document_version_id=uuid4(), version_number=1, chunk_id=uuid4(),
        chunk_index=0, content=content, title="Title", canonical_url=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC), published_at=None, metadata_is_version_snapshot=False, score=1.0,
    )


def test_extract_quote_is_exact_substring_and_prefers_overlap() -> None:
    """The quote is a contiguous chunk substring chosen by lexical overlap with the citing sentence."""
    chunk = ("Cats sleep a lot during the day. " * 8) + "The reactor temperature reached 900 degrees. " + (
        "Dogs bark at night. " * 8)
    quote = citations.extract_supporting_quote("Reactor temperature was high [1].", chunk, 1, max_length=80)
    assert quote in chunk
    assert "reactor" in quote.lower()
    assert len(quote) <= 80


def test_extract_quote_falls_back_without_overlap_and_handles_blank() -> None:
    """No lexical overlap yields the leading characters; a blank chunk yields an empty string."""
    chunk = "x" * 400
    assert citations.extract_supporting_quote("zzz qqq", chunk, 1, max_length=50) == "x" * 50
    assert citations.extract_supporting_quote("anything", "   ") == ""
    assert len(citations.extract_supporting_quote("a", "y" * 10_000, max_length=10**6)) <= MAX_QUOTE_LENGTH


def test_validate_citations_reports_each_rejection_reason_in_order() -> None:
    """Every rejection path keeps its message and the valid citation uses authoritative metadata."""
    item = _evidence("The quick brown fox")
    good = {"documentVersionId": item.document_version_id, "chunkId": item.chunk_id, "quote": "quick brown"}
    missing_ids = {"quote": "x"}
    unknown = {"documentVersionId": uuid4(), "chunkId": uuid4(), "quote": "x"}
    empty_quote = {**good, "quote": ""}
    too_long = {**good, "quote": "q" * (MAX_QUOTE_LENGTH + 1)}
    absent = {**good, "quote": "not in chunk"}
    wrong_doc = {**good, "documentId": uuid4()}
    wrong_source = {**good, "sourceId": uuid4()}
    result = citations.validate_citations(
        [good, missing_ids, unknown, empty_quote, too_long, absent, wrong_doc, wrong_source], [item],
    )
    assert result.is_valid is False
    assert [c.quote for c in result.valid_citations] == ["quick brown"]
    assert isinstance(result.valid_citations[0], Citation)
    reasons = result.rejection_reasons
    assert reasons[0] == "Citation is missing valid documentVersionId or chunkId"
    assert "not in retrieved evidence set" in reasons[1]
    assert reasons[2] == "Citation quote is empty"
    assert "exceeds maximum allowable length" in reasons[3]
    assert reasons[4].startswith("Quote not found in evidence chunk content")
    assert reasons[5].startswith("Document ID")
    assert reasons[6].startswith("Source ID")
    assert len(result.rejected_citations) == 7


def test_date_context_range_variants() -> None:
    """datetime, date and ISO strings map to a half-open day range; garbage maps to none."""
    assert retrieval._date_context_range(datetime(2026, 3, 1, 15, tzinfo=UTC)) == (date(2026, 3, 1), date(2026, 3, 2))
    assert retrieval._date_context_range(date(2026, 3, 31)) == (date(2026, 3, 31), date(2026, 4, 1))
    assert retrieval._date_context_range("2026-03-01") == (date(2026, 3, 1), date(2026, 3, 2))
    assert retrieval._date_context_range("not-a-date") == (None, None)
    assert retrieval._date_context_range(42) == (None, None)


def test_reorder_by_rerank_indices_ignores_bad_and_keeps_unmentioned() -> None:
    """Out-of-range and duplicate indices are skipped; unmentioned items keep their relative order last."""
    items = ["a", "b", "c", "d"]
    assert retrieval._reorder_by_rerank_indices(items, [2, 2, 9, -1, 0]) == ["c", "a", "b", "d"]  # type: ignore[arg-type]


def test_ref_collector_keeps_first_score_and_order() -> None:
    """add_default never overwrites a collected ref and preserves first-seen order."""
    collector = retrieval._RefCollector()
    first, second = (uuid4(), uuid4()), (uuid4(), uuid4())
    collector.refs.append(first)
    collector.scores[first] = 0.9
    collector.add_default(first, 0.5)
    collector.add_default(second, 0.5)
    assert collector.refs == [first, second]
    assert collector.scores == {first: 0.9, second: 0.5}


def test_fit_context_budget_keeps_first_and_enforces_exact_selection() -> None:
    """The first item always fits; an exact selection that does not fit entirely raises."""
    big, small = _evidence("x" * 800), _evidence("y" * 800)
    request = AnswerContextRequest(query="q", context_budget_bytes=1000)
    kept, total = retrieval._fit_context_budget(request, [big, small])
    assert kept == [big] and total == 800
    exact = SimpleNamespace(context_budget_bytes=1000, selected_only=True)
    with pytest.raises(ValueError, match="exceeds the bounded chat context budget"):
        retrieval._fit_context_budget(exact, [big, small])  # type: ignore[arg-type]


def test_observation_filters_always_start_with_permission_predicates() -> None:
    """Workspace and allowed-source predicates come first; optional filters are appended after."""
    args = SimpleNamespace(
        current_only=True, aggregation="none", providers=["A "], metrics=None, symbols=None, region=None,
    )
    filters = db_query._observation_filters(
        args, workspace_id=uuid4(), allowed_source_ids=[uuid4()], date_from=datetime(2026, 1, 1, tzinfo=UTC), date_to=None,  # type: ignore[arg-type]
    )
    assert len(filters) == 5  # workspace, source, is_current, provider, date_from
