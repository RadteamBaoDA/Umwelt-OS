"""Explainable highlight rule evaluation for dashboard gadget streams and feeds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from modules.dashboard.schemas import HighlightRule

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 3,
    "warning": 2,
    "info": 1,
}


@dataclass(frozen=True, slots=True)
class HighlightMatch:
    """Carries the outcome of an evaluated highlight rule against an observed content string.

    Attributes:
        rule_id: Deterministic UUID of the matched highlight rule.
        matched_keywords: Subset of rule keywords detected in the evaluated text.
        severity: Rule severity level ('info', 'warning', or 'critical').
        notify: Whether the rule requested user notification on match.
        reason: Human-readable explainable reason string detailing the match.
    """

    rule_id: UUID
    matched_keywords: tuple[str, ...]
    severity: str
    notify: bool
    reason: str


def evaluate_highlights(text: str, rules: list[HighlightRule]) -> list[HighlightMatch]:
    """Evaluate text content against configured highlight rules and return explainable match results.

    Matches keywords case-insensitively using regex word boundary matching.
    Results are ordered by severity (critical first, then warning, then info).

    Args:
        text: Target document, message, or headline string to evaluate.
        rules: List of validated highlight rules from gadget definition.

    Returns:
        List of HighlightMatch records describing which rules and keywords matched.
    """
    if not text or not rules:
        return []

    matches: list[HighlightMatch] = []

    for rule in rules:
        matched_words: list[str] = []
        for keyword in rule.keywords:
            pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
            if pattern.search(text):
                matched_words.append(keyword)

        if matched_words:
            matched_tuple = tuple(matched_words)
            reason = f"Matched {len(matched_words)} keyword(s): {', '.join(matched_words)}"
            matches.append(
                HighlightMatch(
                    rule_id=rule.id,
                    matched_keywords=matched_tuple,
                    severity=rule.severity,
                    notify=rule.notify,
                    reason=reason,
                )
            )

    # Sort descending by severity precedence
    matches.sort(
        key=lambda m: _SEVERITY_ORDER.get(m.severity, 0),
        reverse=True,
    )
    return matches


def highest_severity(matches: list[HighlightMatch]) -> str | None:
    """Return the highest severity level across a list of highlight matches.

    Args:
        matches: List of evaluated highlight matches.

    Returns:
        'critical', 'warning', 'info', or None if no matches exist.
    """
    if not matches:
        return None
    return matches[0].severity
