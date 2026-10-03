"""Heuristic and ModelGateway-based memory candidate evaluation.

Evaluates novelty, future usefulness, confidence, and explainable selection rationale
against existing active memories. By default, automatic acceptance is off until explicitly
enabled by the owner.
"""

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class CandidateEvaluation:
    """Result of evaluating a memory candidate for novelty, usefulness, and acceptance."""

    novelty_score: float
    usefulness_score: float
    confidence_score: float
    reason: str
    is_duplicate: bool
    should_auto_accept: bool


_TRANSIENT_PATTERNS = [
    re.compile(r"^(hi|hello|hey|good\s+(morning|afternoon|evening)|bye|goodbye)\b", re.I),
    re.compile(r"^(thanks|thank\s+you|ok|okay|yes|no|yep|nope|sure|got\s+it)\b", re.I),
    re.compile(r"^(what|where|when|why|how|who|can\s+you|could\s+you)\b.*\?", re.I),
    re.compile(r"^(test|testing|ping|foo|bar)\b", re.I),
]

_PREFERENCE_PATTERNS = [
    re.compile(r"\b(i\s+prefer|i\s+like|i\s+dislike|i\s+hate|i\s+want|i\s+need|my\s+favorite)\b", re.I),
    re.compile(r"\b(please\s+always|always\s+use|never\s+use|don't\s+use|do\s+not\s+use)\b", re.I),
    re.compile(r"\b(prefer\s+to|rather\s+than)\b", re.I),
]

_INSTRUCTION_PATTERNS = [
    re.compile(r"\b(always\s+(respond|format|include|write|answer))\b", re.I),
    re.compile(r"\b(remember\s+to|make\s+sure\s+to|be\s+concise|keep\s+it\s+short)\b", re.I),
    re.compile(r"\b(rule:|guideline:|instruction:)\b", re.I),
]

_FACT_PATTERNS = [
    re.compile(r"\b(my\s+name\s+is|i\s+am|i\s+live\s+in|i\s+work\s+at|my\s+role\s+is)\b", re.I),
    re.compile(r"\b(my\s+(wife|husband|partner|child|son|daughter|brother|sister|father|mother|dog|cat)\s+is)\b", re.I),
    re.compile(r"\b(remember\s+that|note\s+that|for\s+the\s+record)\b", re.I),
    re.compile(r"\b(we\s+decided\s+to|the\s+plan\s+is\s+to|our\s+project\s+is)\b", re.I),
]

_SPECULATIVE_PATTERNS = [
    re.compile(r"\b(maybe|perhaps|i\s+think|possibly|probably|might|could\s+be)\b", re.I),
    re.compile(r"\b(not\s+sure|guess|speculating)\b", re.I),
]


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase alphanumeric word tokens for set overlap comparison.

    Args:
        text: Input string.

    Returns:
        Set of lowercase string tokens with stopwords excluded.
    """
    stopwords = {"a", "an", "the", "and", "or", "is", "in", "it", "to", "of", "for", "with", "on", "at", "by", "this", "that"}
    words = re.findall(r"\b\w{2,}\b", text.lower())
    return {w for w in words if w not in stopwords}


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity coefficient between two word token sets.

    Args:
        set_a: First token set.
        set_b: Second token set.

    Returns:
        Similarity score between 0.0 and 1.0.
    """
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a.intersection(set_b))
    union = len(set_a.union(set_b))
    return float(intersection) / float(union) if union > 0 else 0.0


def evaluate_candidate(
    content: str,
    memory_type: str,
    existing_memories: list[str],
    *,
    auto_accept_enabled: bool = False,
) -> CandidateEvaluation:
    """Evaluate a candidate memory string for novelty, usefulness, and confidence.

    Args:
        content: Proposed memory content.
        memory_type: Type of memory ('fact', 'preference', 'instruction').
        existing_memories: List of content strings from current active memories.
        auto_accept_enabled: Whether owner has enabled automatic memory persistence.

    Returns:
        CandidateEvaluation containing novelty, usefulness, confidence, and acceptance rationale.
    """
    cleaned = content.strip()
    candidate_tokens = _tokenize(cleaned)

    # 1. Novelty & Deduplication
    max_sim = 0.0
    for existing in existing_memories:
        sim = _jaccard_similarity(candidate_tokens, _tokenize(existing))
        if sim > max_sim:
            max_sim = sim

    if max_sim >= 0.80:
        novelty_score = round(max(0.0, 1.0 - max_sim), 2)
        is_duplicate = True
        novelty_reason = "High overlap with existing active memory."
    elif max_sim >= 0.50:
        novelty_score = round(1.0 - (max_sim * 0.6), 2)
        is_duplicate = False
        novelty_reason = "Related to existing memory but adds new context."
    else:
        novelty_score = 1.0
        is_duplicate = False
        novelty_reason = "Novel information not previously recorded."

    # 2. Future Usefulness
    usefulness_score = 0.5
    usefulness_reason = "Standard contextual usefulness."

    # Check transient
    for pat in _TRANSIENT_PATTERNS:
        if pat.search(cleaned):
            usefulness_score = 0.1
            usefulness_reason = "Transient conversational utterance without durable value."
            break

    if usefulness_score > 0.1:
        # Check high-value patterns
        if any(pat.search(cleaned) for pat in _PREFERENCE_PATTERNS):
            usefulness_score = 0.95
            usefulness_reason = "Owner preference with high future personalization value."
        elif any(pat.search(cleaned) for pat in _INSTRUCTION_PATTERNS):
            usefulness_score = 0.9
            usefulness_reason = "System or operational instruction with high durability."
        elif any(pat.search(cleaned) for pat in _FACT_PATTERNS):
            usefulness_score = 0.85
            usefulness_reason = "Explicit factual statement about owner or projects."

    # 3. Confidence
    confidence_score = 0.8
    if any(pat.search(cleaned) for pat in _SPECULATIVE_PATTERNS):
        confidence_score = 0.4
    elif any(pat.search(cleaned) for pat in _PREFERENCE_PATTERNS + _FACT_PATTERNS + _INSTRUCTION_PATTERNS):
        confidence_score = 0.95

    # 4. Reason summary
    reason_parts = [novelty_reason, usefulness_reason]
    combined_reason = " ".join(reason_parts)

    # 5. Auto-accept determination (default OFF unless owner opted in and high quality)
    should_auto_accept = (
        auto_accept_enabled
        and not is_duplicate
        and novelty_score >= 0.7
        and usefulness_score >= 0.75
        and confidence_score >= 0.85
    )

    return CandidateEvaluation(
        novelty_score=novelty_score,
        usefulness_score=usefulness_score,
        confidence_score=confidence_score,
        reason=combined_reason,
        is_duplicate=is_duplicate,
        should_auto_accept=should_auto_accept,
    )


def extract_candidate_proposals(text: str) -> list[dict[str, Any]]:
    """Scan conversational text to extract candidate memory propositions.

    Splits text by sentence boundaries and identifies clauses containing durable facts,
    preferences, or instructions.

    Args:
        text: User or assistant utterance text.

    Returns:
        List of dicts with 'content', 'type', and 'confidence'.
    """
    proposals: list[dict[str, Any]] = []
    # Split into candidate sentences
    sentences = re.split(r"(?<=[.!?\n])\s+", text)

    for raw in sentences:
        s = raw.strip()
        if len(s) < 10 or len(s) > 1000:
            continue

        # Skip transient greetings and questions
        if any(pat.search(s) for pat in _TRANSIENT_PATTERNS):
            continue

        if any(pat.search(s) for pat in _PREFERENCE_PATTERNS):
            proposals.append({"content": s, "type": "preference", "confidence": 0.9})
        elif any(pat.search(s) for pat in _INSTRUCTION_PATTERNS):
            proposals.append({"content": s, "type": "instruction", "confidence": 0.85})
        elif any(pat.search(s) for pat in _FACT_PATTERNS):
            proposals.append({"content": s, "type": "fact", "confidence": 0.85})

    return proposals
