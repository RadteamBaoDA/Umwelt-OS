from collections.abc import Iterable
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any

from modules.knowledge.entities.schemas import canonicalize_name


def candidate_match_fingerprint(name: str, entity_type: str) -> str:
    """Hash normalized type and name for stable owner-review correction matching."""
    return sha256(f"{entity_type}:{canonicalize_name(name)}".encode()).hexdigest()


def resolve_candidate(name: str, entity_type: str, known: Iterable[dict[str, Any]]) -> tuple[str, str | None, list[str]]:
    """Match only one confirmed alias; name similarity is review-only."""
    # ponytail: compare at most 1,000 same-type entities; overflow stays review-only, DB trigram search if that ceiling matters.
    normalized = canonicalize_name(name)
    alias_matches = [
        item for item in known
        if item.get("type") == entity_type
        and normalized in item.get("confirmed_aliases", [])
    ]
    if len(alias_matches) == 1:
        return "matched", str(alias_matches[0]["id"]), []
    possible = [
        str(item["id"]) for item in known
        if item.get("type") == entity_type
        and isinstance(item.get("name"), str)
        and SequenceMatcher(None, normalized, canonicalize_name(str(item["name"]))).ratio() >= 0.78
    ]
    if alias_matches or possible:
        return "review", None, sorted(set(possible + [str(item["id"]) for item in alias_matches]))
    return "new", None, []
