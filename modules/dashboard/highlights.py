"""Explainable highlight rule evaluation for dashboard gadget streams and feeds."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
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


# ponytail: hard caps bound regex work per item (<= 512 terms/definition, one alternation per rule);
# raise only with a fresh CPU measurement. Stored rules above the caps are truncated at match time.
MAX_TERMS_PER_RULE = 64
MAX_TERMS_PER_DEFINITION = 512


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A rule with its terms resolved and regexes compiled once, reusable across many items."""

    rule: HighlightRule
    terms: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    unresolved: int
    folded: tuple[str, ...] = ()
    gate: re.Pattern[str] | None = None


def compile_rules(
    rules: Sequence[HighlightRule], topic_terms: Mapping[UUID, list[str]] | None = None,
) -> list[CompiledRule]:
    """Resolve topic terms and compile each rule's patterns once; topics absent from the map are unresolved."""
    compiled: list[CompiledRule] = []
    budget = MAX_TERMS_PER_DEFINITION
    for rule in rules:
        terms = list(rule.keywords)
        unresolved = 0
        for topic_id in rule.topic_ids:
            if topic_terms is not None and topic_id in topic_terms:
                terms.extend(topic_terms[topic_id])
            else:
                unresolved += 1
        distinct = tuple(dict.fromkeys(terms))[:min(MAX_TERMS_PER_RULE, budget)]
        budget -= len(distinct)
        gate = re.compile(
            "|".join(rf"\b{re.escape(term)}\b" for term in sorted(distinct, key=len, reverse=True)), re.IGNORECASE,
        ) if distinct else None
        compiled.append(CompiledRule(
            rule, distinct,
            tuple(re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE) for term in distinct), unresolved,
            tuple(term.casefold() for term in distinct), gate,
        ))
    return compiled


def match_compiled(
    text: str, compiled: Sequence[CompiledRule], *, source_id: UUID | None = None,
) -> list[HighlightMatch]:
    """Match text against pre-compiled rules; results are ordered critical first."""
    if not text or not compiled:
        return []
    folded_text = text.casefold()
    matches: list[HighlightMatch] = []
    for item in compiled:
        rule = item.rule
        if source_id is not None and (
            source_id in rule.exclude_source_ids or (rule.source_ids and source_id not in rule.source_ids)
        ):
            continue
        # Cheap substring pre-check, then one alternation pass; only then per-term checks (exact, keeps
        # overlapping terms such as "new york" and "york" both reported).
        if item.gate is None or not any(term in folded_text for term in item.folded) or not item.gate.search(text):
            continue
        matched_words = [
            term for term, folded, pattern in zip(item.terms, item.folded, item.patterns, strict=True)
            if folded in folded_text and pattern.search(text)
        ]
        if matched_words:
            matched_words = matched_words[:16]  # DashboardHighlightRead bound
            reason = f"Matched {len(matched_words)} keyword(s): {', '.join(matched_words)}"
            if item.unresolved:
                reason += f" ({item.unresolved} topic(s) unavailable)"
            matches.append(HighlightMatch(
                rule_id=rule.id, matched_keywords=tuple(matched_words), severity=rule.severity,
                notify=rule.notify, reason=reason,
            ))
    matches.sort(key=lambda m: _SEVERITY_ORDER.get(m.severity, 0), reverse=True)
    return matches


def _minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:])


def notification_allowed(
    rule: HighlightRule, now: datetime, tz: tzinfo, last_notified: datetime | None,
) -> bool:
    """Decide delivery only: expired, quiet-hours (suppressed, not deferred) and cooldown skip the notification."""
    if rule.expires_at is not None and now >= rule.expires_at:
        return False
    if rule.quiet_start is not None and rule.quiet_end is not None:
        local = now.astimezone(tz)
        current, start, end = local.hour * 60 + local.minute, _minutes(rule.quiet_start), _minutes(rule.quiet_end)
        if (start <= current < end) if start < end else (current >= start or current < end):
            return False
    return not (
        rule.cooldown_minutes and last_notified is not None
        and now - last_notified < timedelta(minutes=rule.cooldown_minutes)
    )


def evaluate_highlights(
    text: str, rules: list[HighlightRule], *,
    source_id: UUID | None = None, topic_terms: Mapping[UUID, list[str]] | None = None,
) -> list[HighlightMatch]:
    """Evaluate text against highlight rules and return explainable matches (compiles per call).

    Matches terms case-insensitively with word boundaries. Per-rule include/exclude source filters apply
    to ``source_id``. A topic id absent from ``topic_terms`` is unresolved: it never matches and is named
    in the reason. Loops over many items should call ``compile_rules`` once and ``match_compiled``.
    """
    if not text or not rules:
        return []
    return match_compiled(text, compile_rules(rules, topic_terms), source_id=source_id)


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
