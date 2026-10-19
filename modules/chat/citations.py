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
UNCITED_ANSWER_NOTICE = "[Uncited] "

_WORD_RE = re.compile(r"[a-z0-9\u00c0-\u024f\u1e00-\u1eff]{2,}", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "what", "all", "can", "will", "into",
    "your", "about", "there", "their", "which", "when", "where", "who", "how",
    "why", "out", "use", "used", "using", "than", "then", "also", "some", "such",
    "more", "other", "its", "our", "you", "they", "them", "these", "those",
    "would", "could", "should", "may", "might", "must", "shall", "been", "being",
    "does", "did", "doing", "done", "each", "any", "only", "very", "just", "over",
    "under", "between", "after", "before", "through", "during", "without", "within",
    "của", "và", "các", "có", "trong", "được", "cho", "là", "với", "không", "những",
    "một", "này", "đã", "khi", "về", "như", "từ", "theo", "đến", "hay", "hoặc",
})


_NUM = r"\d{1,4}(?:\s*,\s*\d{1,4})*"
# A run of markers ([1], [1][3], [1, 3]); rejects identifier indexing, URL paths and markdown links.
_MARKER_RE = re.compile(rf"( ?)(?<![\w\]/(])(\[{_NUM}\](?:\[{_NUM}\])*)(?![(\[])")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def _outside_code(text: str) -> list[tuple[str, bool]]:
    """Split text into (segment, is_prose) pairs; fenced and inline code are not prose."""
    return [(part, i % 2 == 0) for i, part in enumerate(re.split(f"({_CODE_RE.pattern})", text, flags=re.DOTALL))]


def _run_numbers(run: str) -> list[int]:
    """Extract all integer citation numbers from a bracketed marker run."""
    return [int(n) for n in re.findall(r"\d+", run)]


def _tokenize_content_words(text: str) -> set[str]:
    """Tokenize text into lowercase alphanumeric content words excluding common stopwords."""
    cleaned = _MARKER_RE.sub("", text)
    return {
        tok
        for tok in (m.group(0).lower() for m in _WORD_RE.finditer(cleaned))
        if tok not in _STOPWORDS and not tok.isdigit()
    }


def extract_supporting_quote(
    answer: str,
    chunk_content: str,
    citation_number: int | None = None,
    max_length: int = 240,
) -> str:
    """Extract an exact contiguous supporting quote from chunk_content for a cited claim.

    Locates the sentence(s) in ``answer`` referencing ``[citation_number]`` (or uses the full
    ``answer`` when ``citation_number`` is omitted or not found), tokenizes into lowercase content
    words, and scores candidate sentences/windows in ``chunk_content`` by lexical overlap. Returns
    an exact contiguous substring of ``chunk_content`` bounded by ``max_length`` (and ``MAX_QUOTE_LENGTH``)
    so downstream ``_contains_quote`` checks are guaranteed to succeed. Falls back to the leading
    200 characters of ``chunk_content`` when there is no lexical overlap or the chunk is already short.

    Args:
        answer: Generated response text containing claims and optional bracketed citation markers.
        chunk_content: Full text of the retrieved evidence chunk.
        citation_number: Optional 1-based evidence number whose citing sentence(s) should be matched.
        max_length: Maximum character length of the returned contiguous substring.

    Returns:
        An exact contiguous non-empty substring of ``chunk_content`` (or empty string if chunk is blank).
    """
    stripped_chunk = chunk_content.strip()
    if not stripped_chunk:
        return ""

    effective_max = max(1, min(max_length, MAX_QUOTE_LENGTH))
    fallback = stripped_chunk[: min(200, effective_max)].strip()
    if len(stripped_chunk) <= min(200, effective_max):
        return stripped_chunk

    # Identify sentences in the answer that cite citation_number, falling back to full answer.
    target_sentences: list[str] = []
    if citation_number is not None and answer:
        for sent in _SENTENCE_SPLIT_RE.split(answer):
            if not sent.strip():
                continue
            for match in _MARKER_RE.finditer(sent):
                if citation_number in _run_numbers(match.group(2)):
                    target_sentences.append(sent)
                    break
    query_text = " ".join(target_sentences) if target_sentences else (answer or "")
    query_tokens = _tokenize_content_words(query_text)
    if not query_tokens and answer and target_sentences:
        query_tokens = _tokenize_content_words(answer)
    if not query_tokens:
        return fallback

    # Partition chunk_content into sentence-like spans while preserving exact character offsets.
    spans: list[tuple[int, int]] = []
    last_end = 0
    for split_match in _SENTENCE_SPLIT_RE.finditer(stripped_chunk):
        if split_match.start() > last_end:
            spans.append((last_end, split_match.start()))
        last_end = split_match.end()
    if last_end < len(stripped_chunk):
        spans.append((last_end, len(stripped_chunk)))

    if not spans:
        return fallback

    best_quote = ""
    best_score = 0

    # Evaluate individual sentence spans and pairs of adjacent spans that fit within effective_max.
    candidate_ranges: list[tuple[int, int]] = list(spans)
    for i in range(len(spans) - 1):
        pair_start, pair_end = spans[i][0], spans[i + 1][1]
        if pair_end - pair_start <= effective_max:
            candidate_ranges.append((pair_start, pair_end))

    for start_idx, end_idx in candidate_ranges:
        segment = stripped_chunk[start_idx:end_idx].strip()
        if not segment:
            continue
        if len(segment) <= effective_max:
            seg_tokens = _tokenize_content_words(segment)
            overlap = len(query_tokens & seg_tokens)
            if overlap > best_score:
                best_score = overlap
                best_quote = segment
        else:
            # Slide a character window across a long span at word boundaries to stay <= effective_max.
            step = max(20, effective_max // 3)
            for offset in range(start_idx, end_idx, step):
                win_end = min(end_idx, offset + effective_max)
                window = stripped_chunk[offset:win_end].strip()
                if not window:
                    continue
                win_tokens = _tokenize_content_words(window)
                overlap = len(query_tokens & win_tokens)
                if overlap > best_score:
                    best_score = overlap
                    best_quote = window
                if win_end >= end_idx:
                    break

    if best_score <= 0 or not best_quote:
        return fallback
    return best_quote


def post_hoc_attribute_citations(
    answer: str,
    evidence: list[EvidenceItem],
    *,
    min_shared_tokens: int = 3,
    min_overlap_ratio: float = 0.35,
) -> tuple[str, list[Citation]]:
    """Perform post-hoc lexical attribution when an answer omitted inline citation markers.

    Checks whether any retrieved ``EvidenceItem`` shares strong lexical overlap with ``answer``
    (at least ``min_shared_tokens`` content words or at least ``min_overlap_ratio`` of the answer's
    content words with >= 2 shared tokens). When a strong match is found, attaches ``[1]`` to the
    answer and constructs a ``Citation`` with ``extract_supporting_quote`` so grounded streamed
    answers are preserved with a verified citation instead of being wiped.

    Args:
        answer: Generated response text lacking inline citation markers.
        evidence: Ordered list of retrieved evidence items for the turn.
        min_shared_tokens: Minimum shared non-stopword content tokens to qualify as grounded.
        min_overlap_ratio: Minimum fraction of answer content tokens matched in the chunk.

    Returns:
        Tuple of (annotated_answer, candidate_citations). Returns (answer, []) if no chunk matches.
    """
    if not answer or not answer.strip() or not evidence:
        return answer, []

    lower_ans = answer.lower()
    if (
        "insufficient evidence" in lower_ans
        or "not enough information" in lower_ans
        or "cannot answer" in lower_ans
        or "do not have sufficient" in lower_ans
    ):
        return answer, []

    answer_tokens = _tokenize_content_words(answer)
    if len(answer_tokens) < 2:
        return answer, []

    best_item: EvidenceItem | None = None
    best_overlap = 0
    best_ratio = 0.0

    for item in evidence:
        if not item.content or not item.content.strip():
            continue
        chunk_tokens = _tokenize_content_words(item.content)
        if not chunk_tokens:
            continue
        shared = len(answer_tokens & chunk_tokens)
        ratio = shared / len(answer_tokens)
        if (shared >= min_shared_tokens or (shared >= 2 and ratio >= min_overlap_ratio)) and (
            shared > best_overlap or (shared == best_overlap and ratio > best_ratio)
        ):
            best_overlap = shared
            best_ratio = ratio
            best_item = item

    if best_item is None:
        return answer, []

    quote = extract_supporting_quote(answer, best_item.content, citation_number=1)
    if not quote:
        return answer, []

    citation = Citation(
        sourceType="document",
        sourceId=best_item.source_id,
        documentId=best_item.document_id,
        documentVersionId=best_item.document_version_id,
        chunkId=best_item.chunk_id,
        title=best_item.title,
        url=best_item.canonical_url,
        observedAt=best_item.observed_at,
        quote=quote,
    )
    stripped = answer.rstrip()
    if stripped and stripped[-1] in ".!?":
        annotated = f"{stripped[:-1]} [1]{stripped[-1]}"
    else:
        annotated = f"{stripped} [1]"
    return annotated, [citation]


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
    has_sufficient_evidence: bool = False,
    allow_uncited_conversational: bool = False,
) -> str:
    """Ensure that answers without sufficient evidence or citations explicitly disclose the limitation.

    When ``allow_uncited_conversational`` is enabled for a streamed conversational turn that has no
    matching citations (and no broken citation markers), prefixes ``[Uncited] `` to preserve the
    already-streamed response with a clear uncited disclosure rather than wiping it.

    Args:
        answer: Raw generated answer text.
        citations: List of verified citations.
        has_sufficient_evidence: Whether the retrieved evidence was deemed sufficient.
        allow_uncited_conversational: Whether to annotate an uncited conversational answer instead of wiping.

    Returns:
        The original answer if supported, an ``[Uncited]``-prefixed answer when allowed, or an explicit
        insufficient-evidence response.
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
        if allow_uncited_conversational and answer.strip():
            if answer.lstrip().startswith(UNCITED_ANSWER_NOTICE.strip()):
                return answer
            return f"{UNCITED_ANSWER_NOTICE}{answer}"
        return INSUFFICIENT_EVIDENCE_MESSAGE
    return answer


def validate_answer_citations(
    answer: str,
    citations: list[Citation | dict[str, Any]],
    evidence: list[EvidenceItem] | dict[tuple[UUID, UUID], EvidenceItem],
    *,
    allow_uncited_conversational: bool = False,
) -> ValidatedAnswer:
    """Validate answer citations against evidence and enforce grounded response disclosures.

    If ``citations`` is empty but ``evidence`` is non-empty, performs a post-hoc lexical attribution
    check to attach ``[1]`` and its supporting quote when the answer has strong lexical overlap
    (>= 3 shared content words or high overlap ratio) with a retrieved evidence chunk.

    Args:
        answer: Candidate response text from language model.
        citations: Candidate citations list.
        evidence: Retrieved evidence set for the query.
        allow_uncited_conversational: Whether to preserve uncited conversational answers with an
            ``[Uncited]`` notice when no citations were proposed or rejected.

    Returns:
        ValidatedAnswer instance containing verified citations and grounded answer text.
    """
    effective_answer = answer
    effective_citations = list(citations)

    evidence_list = list(evidence.values()) if isinstance(evidence, dict) else list(evidence)
    if not effective_citations and evidence_list:
        attributed_answer, attributed_citations = post_hoc_attribute_citations(answer, evidence_list)
        if attributed_citations:
            effective_answer = attributed_answer
            effective_citations = list(attributed_citations)

    validation = validate_citations(effective_citations, evidence)
    has_sufficient = len(validation.valid_citations) > 0 and (
        bool(evidence) if isinstance(evidence, (list, dict)) else False
    )
    can_allow_uncited = (
        allow_uncited_conversational
        and not citations
        and len(validation.rejected_citations) == 0
    )
    final_answer = ensure_grounded_answer(
        effective_answer,
        validation.valid_citations,
        has_sufficient_evidence=has_sufficient,
        allow_uncited_conversational=can_allow_uncited,
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

