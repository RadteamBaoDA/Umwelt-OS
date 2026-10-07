"""Pure marker parsing/renumbering and cited-only validation for chat answers."""

from datetime import UTC, datetime
from uuid import uuid4

from modules.chat.citations import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    parse_citation_markers,
    renumber_citation_markers,
    validate_answer_citations,
)
from modules.chat.schemas import Citation, EvidenceItem


def test_parse_variants_range_dedupe_and_order() -> None:
    assert parse_citation_markers("a [3] b [1][2] c [2, 3] d [1,3]", 3) == [3, 1, 2]
    assert parse_citation_markers("x [0] [4] [99] [a] [1.5]", 3) == []
    assert parse_citation_markers("x [4, 2]", 3) == [2]
    assert parse_citation_markers("none", 3) == []


def test_renumber_to_position_and_drop_unmapped() -> None:
    mapping = {3: 1, 1: 2}
    assert renumber_citation_markers("a [3] b [1][3] c [1, 3] d [2] e [9]", mapping, 3) == "a [1] b [2][1] c [2][1] d e [9]"
    assert renumber_citation_markers("ok [2][5].", {}, 5) == "ok."
    assert renumber_citation_markers("d [2] e", {}, 3) == "d e"


def test_markers_ignore_code_links_years_and_huge_numbers() -> None:
    text = "Use `arr[0]` and `x[1]`; in [2023] see [docs](http://a/[1]) and [1](u) and [2]."
    assert parse_citation_markers(text, 3) == [2]
    assert renumber_citation_markers(text, {2: 1}, 3) == (
        "Use `arr[0]` and `x[1]`; in [2023] see [docs](http://a/[1]) and [1](u) and [1]."
    )
    fenced = "```py\nx = a[1]\n```\nok [1]"
    assert parse_citation_markers(fenced, 3) == [1]
    assert renumber_citation_markers(fenced, {1: 2}, 3) == "```py\nx = a[1]\n```\nok [2]"
    assert parse_citation_markers("[" + "9" * 5000 + "]", 3) == []
    assert renumber_citation_markers("[" + "9" * 5000 + "]", {}, 3) == "[" + "9" * 5000 + "]"


def _item(text: str) -> EvidenceItem:
    return EvidenceItem(
        source_id=uuid4(), source_generation=1, local_only=False, document_id=uuid4(), document_version_id=uuid4(),
        version_number=1, chunk_id=uuid4(), content=text, title="T", observed_at=datetime.now(UTC),
    )


def test_only_cited_evidence_and_uncited_answer_disclosed() -> None:
    items = [_item("alpha"), _item("beta"), _item("gamma")]
    cited = parse_citation_markers("Beta is true [2].", len(items))
    cands = [
        Citation(
            sourceType="document", sourceId=i.source_id, documentId=i.document_id,
            documentVersionId=i.document_version_id, chunkId=i.chunk_id, title=i.title,
            url=None, observedAt=i.observed_at, quote=i.content,
        )
        for i in (items[n - 1] for n in cited)
    ]
    res = validate_answer_citations("Beta is true [2].", cands, items)
    assert [c.chunkId for c in res.citations] == [items[1].chunk_id]
    res = validate_answer_citations("Beta is true.", [], items)
    assert res.citations == [] and res.answer == INSUFFICIENT_EVIDENCE_MESSAGE
