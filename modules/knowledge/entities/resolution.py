from collections.abc import Iterable
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any

from modules.knowledge.entities.schemas import canonicalize_name


def candidate_match_fingerprint(name: str, entity_type: str) -> str:
    """Hash normalized type and name for stable owner-review correction matching."""
    return sha256(f"{entity_type}:{canonicalize_name(name)}".encode()).hexdigest()


_SIMILARITY_THRESHOLD = 0.78


def _name_similarity_matches(
    candidate_norm: str,
    alias_norm: str,
    *,
    threshold: float = _SIMILARITY_THRESHOLD,
) -> bool:
    """Return whether two canonicalized entity names meet the fuzzy similarity threshold.

    Applies exact equality and ``SequenceMatcher`` upper-bound filters (length ratio,
    ``real_quick_ratio``, and character-multiset ``quick_ratio``) before computing the
    quadratic contiguous-block ``ratio()``. Because ``ratio() <= quick_ratio() <=
    real_quick_ratio()``, this short-circuiting produces mathematically identical results
    to ``SequenceMatcher(None, candidate_norm, alias_norm).ratio() >= threshold`` while
    rejecting length- or character-incompatible names in O(1) / O(N + M) time.
    """
    if candidate_norm == alias_norm:
        return True
    len_a = len(candidate_norm)
    len_b = len(alias_norm)
    if len_a == 0 or len_b == 0:
        return False
    # SequenceMatcher.ratio() is 2 * M / (L1 + L2) with M <= min(L1, L2); skip immediately
    # when the mathematical length upper bound cannot reach the similarity threshold.
    if (2.0 * min(len_a, len_b)) / (len_a + len_b) < threshold:
        return False
    matcher = SequenceMatcher(None, candidate_norm, alias_norm)
    return (
        matcher.real_quick_ratio() >= threshold
        and matcher.quick_ratio() >= threshold
        and matcher.ratio() >= threshold
    )


def resolve_candidate(name: str, entity_type: str, known: Iterable[dict[str, Any]]) -> tuple[str, str | None, list[str]]:
    """Match only one confirmed alias; name similarity is review-only."""
    # ponytail: compare at most 1,000 same-type entities; overflow stays review-only, DB trigram search if that ceiling matters.
    normalized = canonicalize_name(name)
    same_type = [item for item in known if item.get("type") == entity_type]
    alias_matches = [
        item for item in same_type
        if normalized in item.get("confirmed_aliases", [])
    ]
    if len(alias_matches) == 1:
        return "matched", str(alias_matches[0]["id"]), []
    possible = [
        str(item["id"]) for item in same_type
        if isinstance(item.get("name"), str)
        and _name_similarity_matches(normalized, canonicalize_name(str(item["name"])))
    ]
    if alias_matches or possible:
        return "review", None, sorted(set(possible + [str(item["id"]) for item in alias_matches]))
    return "new", None, []
