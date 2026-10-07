"""Explainable highlight rule evaluation for dashboard gadget streams and feeds."""

from __future__ import annotations

import re
from collections.abc import Mapping
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


def evaluate_highlights(
    text: str, rules: list[HighlightRule], *,
    source_id: UUID | None = None, topic_terms: Mapping[UUID, list[str]] | None = None,
) -> list[HighlightMatch]:
    """Evaluate text content against configured highlight rules and return explainable match results.

    Matches keywords case-insensitively using regex word boundary matching.
    Results are ordered by severity (critical first, then warning, then info).

    Args:
        text: Target document, message, or headline string to evaluate.
        rules: List of validated highlight rules from gadget definition.
        source_id: Source of the evaluated item; per-rule include/exclude source filters apply to it.
        topic_terms: Resolved keywords and entity names per topic id. A topic id absent from this
            mapping is unresolved: it never matches and is named in the match reason.

    Returns:
        List of HighlightMatch records describing which rules and keywords matched.
    """
    if not text or not rules:
        return []

    matches: list[HighlightMatch] = []

    for rule in rules:
        if source_id is not None and (
            source_id in rule.exclude_source_ids or (rule.source_ids and source_id not in rule.source_ids)
        ):
            continue
        terms = list(rule.keywords)
        unresolved = 0
        for topic_id in rule.topic_ids:
            if topic_terms is not None and topic_id in topic_terms:
                terms.extend(topic_terms[topic_id])
            else:
                unresolved += 1
        matched_words: list[str] = []
        for keyword in dict.fromkeys(terms):
            pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
            if pattern.search(text):
                matched_words.append(keyword)

        if matched_words:
            matched_words = matched_words[:16]  # DashboardHighlightRead bound
            matched_tuple = tuple(matched_words)
            reason = f"Matched {len(matched_words)} keyword(s): {', '.join(matched_words)}"
            if unresolved:
                reason += f" ({unresolved} topic(s) unavailable)"
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
