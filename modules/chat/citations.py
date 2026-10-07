"""Citation validation and verification against detached retrieved evidence sets."""

import re
from typing import Any
from uuid import UUID

from modules.chat.schemas import (
    MAX_QUOTE_LENGTH,
    Citation,
    CitationValidationResult,
    EvidenceItem,
    ValidatedAnswer,
)

INSUFFICIENT_EVIDENCE_MESSAGE = (
    "I do not have sufficient evidence in the retrieved documents to answer this question."
)


_NUM = r"\d{1,4}(?:\s*,\s*\d{1,4})*"
# A run of markers ([1], [1][3], [1, 3]); rejects identifier indexing, URL paths and markdown links.
_MARKER_RE = re.compile(rf"( ?)(?<![\w\]/(])(\[{_NUM}\](?:\[{_NUM}\])*)(?![(\[])")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def _outside_code(text: str) -> list[tuple[str, bool]]:
    """Split text into (segment, is_prose) pairs; fenced and inline code are not prose."""
    return [(part, i % 2 == 0) for i, part in enumerate(re.split(f"({_CODE_RE.pattern})", text, flags=re.DOTALL))]


def _run_numbers(run: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", run)]


def parse_citation_markers(answer: str, evidence_count: int) -> list[int]:
    """Return 1-based evidence numbers cited in the answer, first-cited order, deduplicated.

    Recognises ``[n]``, ``[n][m]`` and ``[n, m]`` outside code; numbers outside 1..evidence_count are ignored.
    """
    seen: dict[int, None] = {}
    for part, prose in _outside_code(answer):
        if not prose:
            continue
        for match in _MARKER_RE.finditer(part):
            for n in _run_numbers(match.group(2)):
                if 1 <= n <= evidence_count:
                    seen.setdefault(n, None)
    return list(seen)


def renumber_citation_markers(answer: str, mapping: dict[int, int], evidence_count: int) -> str:
    """Rewrite citation marker runs through ``mapping`` (old number -> new number) as ``[k][j]``.

    A run with at least one number in 1..evidence_count is a citation: numbers absent from the
    mapping are removed and an emptied run is dropped with one leading space. Other bracketed
    integers (``[2023]``) and code regions are left untouched.
    """
    def repl(match: re.Match[str]) -> str:
        nums = _run_numbers(match.group(2))
        if not any(1 <= n <= evidence_count for n in nums):
            return match.group(0)
        new = dict.fromkeys(mapping[n] for n in nums if n in mapping)
        kept = "".join(f"[{k}]" for k in new)
        return match.group(1) + kept if kept else ""

    return "".join(_MARKER_RE.sub(repl, part) if prose else part for part, prose in _outside_code(answer))


def _normalize_whitespace(text: str) -> str:
    """Collapse consecutive whitespace characters into a single space for comparison."""
    return re.sub(r"\s+", " ", text).strip()


def _contains_quote(quote: str, content: str) -> bool:
    """Check if the quote substring is contained in chunk content with whitespace tolerance."""
    if not quote or not content:
        return False
    if quote in content:
        return True
    return _normalize_whitespace(quote) in _normalize_whitespace(content)


def _to_uuid(val: Any) -> UUID | None:
    """Safely convert a string or UUID object to a UUID instance, returning None on error."""
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        try:
            return UUID(val)
        except (ValueError, AttributeError):
            return None
    return None


def validate_citations(
    citations: list[Citation | dict[str, Any]],
    evidence: list[EvidenceItem] | dict[tuple[UUID, UUID], EvidenceItem],
) -> CitationValidationResult:
    """Validate citation references against the detached retrieved evidence set.

    Rejects citations not in the retrieved evidence set, mismatched document/source IDs,
    quotes exceeding length bounds, and quotes not contained within the backing chunk.
    Authoritative metadata (title, canonical URL, observed timestamp) is always taken
    from the retrieved evidence to prevent client-forged metadata.

    Args:
        citations: List of Citation instances or raw citation dictionaries.
        evidence: List of EvidenceItem instances or lookup dictionary keyed by (version_id, chunk_id).

    Returns:
        CitationValidationResult detailing valid citations and rejected citations with reasons.
    """
    if isinstance(evidence, dict):
        evidence_map = evidence
    else:
        evidence_map = {
            (item.document_version_id, item.chunk_id): item for item in evidence
        }

    valid_citations: list[Citation] = []
    rejected_citations: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []

    for raw in citations:
        raw_dict = raw.model_dump(by_alias=True) if isinstance(raw, Citation) else dict(raw)

        # Extract identifiers from either camelCase or snake_case keys
        source_id = _to_uuid(raw_dict.get("sourceId") or raw_dict.get("source_id"))
        doc_id = _to_uuid(raw_dict.get("documentId") or raw_dict.get("document_id"))
        version_id = _to_uuid(raw_dict.get("documentVersionId") or raw_dict.get("document_version_id"))
        chunk_id = _to_uuid(raw_dict.get("chunkId") or raw_dict.get("chunk_id"))
        quote = str(raw_dict.get("quote") or "").strip()

        if not version_id or not chunk_id:
            rejected_citations.append(raw_dict)
            rejection_reasons.append("Citation is missing valid documentVersionId or chunkId")
            continue

        evidence_item = evidence_map.get((version_id, chunk_id))
        if evidence_item is None:
            rejected_citations.append(raw_dict)
            rejection_reasons.append(
                f"Reference (version={version_id}, chunk={chunk_id}) not in retrieved evidence set"
            )
            continue

        if doc_id and doc_id != evidence_item.document_id:
            rejected_citations.append(raw_dict)
            rejection_reasons.append(
                f"Document ID {doc_id} does not match evidence document ID {evidence_item.document_id}"
            )
            continue

        if source_id and source_id != evidence_item.source_id:
            rejected_citations.append(raw_dict)
            rejection_reasons.append(
                f"Source ID {source_id} does not match evidence source ID {evidence_item.source_id}"
            )
            continue

        if not quote:
            rejected_citations.append(raw_dict)
            rejection_reasons.append("Citation quote is empty")
            continue

        if len(quote) > MAX_QUOTE_LENGTH:
            rejected_citations.append(raw_dict)
            rejection_reasons.append(
                f"Citation quote length ({len(quote)}) exceeds maximum allowable length ({MAX_QUOTE_LENGTH})"
            )
            continue

        if not _contains_quote(quote, evidence_item.content):
            rejected_citations.append(raw_dict)
            rejection_reasons.append(
                f"Quote not found in evidence chunk content for chunk {chunk_id}"
            )
            continue

        # Construct authoritative citation using evidence metadata
        canonical_citation = Citation(
            sourceType="document",
            sourceId=evidence_item.source_id,
            documentId=evidence_item.document_id,
            documentVersionId=evidence_item.document_version_id,
            chunkId=evidence_item.chunk_id,
            title=evidence_item.title,
            url=evidence_item.canonical_url,
            observedAt=evidence_item.observed_at,
            quote=quote,
        )
        valid_citations.append(canonical_citation)

    is_valid = len(rejected_citations) == 0 and len(valid_citations) > 0
    return CitationValidationResult(
        is_valid=is_valid,
        valid_citations=valid_citations,
        rejected_citations=rejected_citations,
        rejection_reasons=rejection_reasons,
    )


def ensure_grounded_answer(
    answer: str,
    citations: list[Citation],
    *,
    has_sufficient_evidence: bool,
) -> str:
    """Ensure that answers without sufficient evidence or citations explicitly disclose the limitation.

    Args:
        answer: Raw generated answer text.
        citations: List of verified citations.
        has_sufficient_evidence: Whether the retrieved evidence was deemed sufficient.

    Returns:
        The original answer if supported, or an explicit insufficient-evidence response.
    """
    if not has_sufficient_evidence or not citations:
        lower_ans = answer.lower()
        if (
            "insufficient evidence" in lower_ans
            or "not enough information" in lower_ans
            or "cannot answer" in lower_ans
            or "do not have sufficient" in lower_ans
        ):
            return answer
        return INSUFFICIENT_EVIDENCE_MESSAGE
    return answer


def validate_answer_citations(
    answer: str,
    citations: list[Citation | dict[str, Any]],
    evidence: list[EvidenceItem] | dict[tuple[UUID, UUID], EvidenceItem],
) -> ValidatedAnswer:
    """Validate answer citations against evidence and enforce grounded response disclosures.

    Args:
        answer: Candidate response text from language model.
        citations: Candidate citations list.
        evidence: Retrieved evidence set for the query.

    Returns:
        ValidatedAnswer instance containing verified citations and grounded answer text.
    """
    validation = validate_citations(citations, evidence)
    has_sufficient = len(validation.valid_citations) > 0 and (
        bool(evidence) if isinstance(evidence, (list, dict)) else False
    )
    final_answer = ensure_grounded_answer(
        answer, validation.valid_citations, has_sufficient_evidence=has_sufficient
    )
    warnings = list(validation.rejection_reasons)
    if not has_sufficient:
        warnings.append("Answer lacks sufficient verified citations")

    return ValidatedAnswer(
        answer=final_answer,
        citations=validation.valid_citations,
        has_sufficient_evidence=has_sufficient,
        warnings=warnings,
    )
