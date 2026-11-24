"""Pure recall, scope-leak and hidden-miss grading for the chat real-model eval (no I/O).

Ids are compared as strings: tool JSON carries UUIDs as strings and the validated citations use the same form.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from tests.integration.chat_eval.cases import Case

_ID_KEYS = ("observations", "documents", "rows")
# Cue words that mark a figure as excluded, wrong or superseded (English and Vietnamese).
_EXCLUSION_CUE = re.compile(
    r"\b(?:not|no|never|n't|exclud\w*|outside|below|earlier|previous|wrong|correct\w*)\b|n't|"
    r"không|chưa|ngoài|nhầm|sai|trước|đính chính|sửa",
    re.IGNORECASE,
)
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class RowIds:
    """Document version ids and source ids found in one tool result."""

    version_ids: set[str]
    source_ids: set[str]


def _dict_rows(value: Any) -> Iterable[dict[str, Any]]:
    """Yield the dict rows of a list-valued payload field; anything else yields nothing."""
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def extract_row_ids(result: Mapping[str, Any]) -> RowIds:
    """Collect ``document_version_id`` and ``source_id`` from a query-tool payload.

    Reads ``observations``, ``documents`` and ``rows``. Structured observations and documents do not serialise
    these ids (they carry only ``[n]`` citation markers), so they contribute nothing; the caller recovers those
    from the turn's evidence list. ``observation_aggregates`` is ignored. Pure; never raises on odd shapes.
    """
    version_ids: set[str] = set()
    source_ids: set[str] = set()
    for key in _ID_KEYS:
        for row in _dict_rows(result.get(key)):
            if row.get("document_version_id") is not None:
                version_ids.add(str(row["document_version_id"]))
            if row.get("source_id") is not None:
                source_ids.add(str(row["source_id"]))
    return RowIds(version_ids, source_ids)


def recall(gold: set[str], got: set[str]) -> float | None:
    """Share of ``gold`` ids present in ``got``; None when there is no gold to measure against."""
    if not gold:
        return None
    return len(gold & got) / len(gold)


def scope_ids(case: Case, sources: Mapping[str, UUID]) -> list[str] | None:
    """The case's source scope as id strings, or None when the case is unscoped.

    Raises KeyError for a scope key the matrix did not seed, so a typo in a case fails loudly instead of
    silently widening the scope to the whole workspace.
    """
    if not case.source_scope:
        return None
    return [str(sources[key]) for key in case.source_scope]


def min_recall_ok(min_recall: float | None, retrieval: float | None) -> bool | None:
    """True when retrieval met the case's ``min_recall``; None when either side is unset (no gold or no floor)."""
    if min_recall is None or retrieval is None:
        return None
    return retrieval >= min_recall


def scope_leak(
    source_ids: set[str],
    allowed: set[str] | None,
    forbidden_versions: set[str],
    got_versions: set[str],
) -> bool:
    """True when a retrieved source falls outside ``allowed`` or a forbidden version was retrieved.

    ``allowed`` None means the case has no source scope, so only the forbidden versions are checked.
    """
    if allowed is not None and source_ids - allowed:
        return True
    return bool(forbidden_versions & got_versions)


def tool_used(
    leg: str,
    called: bool,
    retrieved_versions: set[str],
    *,
    expects_no_tool: bool = False,
) -> bool | None:
    """True when the turn used collected data: a query-tool call, or on the hybrid leg any build_context evidence.

    The hybrid leg has no tool; its evidence is supplied by ``build_context`` before the model runs, so
    retrieved versions there count as using the data. When ``expects_no_tool`` is set (refusals and local-only
    questions with no collected source) the criterion does not apply and None is returned. Pure.
    """
    if expects_no_tool:
        return None
    return called or (leg == "hybrid" and bool(retrieved_versions))


def hidden_miss(
    criteria: Mapping[str, Any], retrieval_recall: float | None, answer_recall: float | None
) -> bool:
    """True when no existing criterion failed yet a recall leg fell short of 1.0.

    That is the case the answer passes on its face but missed gold data: it would look fine without the recall
    columns. None recalls (no gold) are ignored. A ``scope_leak`` of True explains the miss, so it is never a
    hidden miss; a ``scope_leak`` of False (no leak) is not a failed criterion and does not block it.
    """
    if criteria.get("scope_leak") is True:
        return False
    if any(value is False for key, value in criteria.items() if key != "scope_leak"):
        return False
    return any(r is not None and r < 1 for r in (retrieval_recall, answer_recall))


def _mean(values: list[float]) -> float | None:
    """Mean rounded to 3 places, or None for an empty list."""
    return round(sum(values) / len(values), 3) if values else None


def summarize_recall(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recall, scope-leak and hidden-miss criteria per category and overall (pure).

    Each entry needs ``category`` and ``criteria``; entries without a criteria dict are skipped. Output keys:
    overall ``retrieval_recall`` / ``answer_recall`` means, ``scope_leaks`` and ``hidden_miss`` totals, and
    ``recall_by_category`` with the same four measures per category (``retrieval``, ``answer``, ``scope_leaks``,
    ``hidden_miss``).
    """
    graded = [e for e in entries if isinstance(e.get("criteria"), dict)]
    by_cat: dict[str, dict[str, list[Any]]] = {}
    for e in graded:
        slot = by_cat.setdefault(
            e["category"], {"retrieval": [], "answer": [], "leak": [], "miss": []}
        )
        crit = e["criteria"]
        slot["retrieval"].append(crit.get("retrieval_recall"))
        slot["answer"].append(crit.get("answer_recall"))
        slot["leak"].append(bool(crit.get("scope_leak")))
        slot["miss"].append(bool(crit.get("hidden_miss")))
    retrieval = [
        e["criteria"]["retrieval_recall"]
        for e in graded
        if e["criteria"].get("retrieval_recall") is not None
    ]
    answer = [
        e["criteria"]["answer_recall"]
        for e in graded
        if e["criteria"].get("answer_recall") is not None
    ]
    return {
        "retrieval_recall": _mean(retrieval),
        "answer_recall": _mean(answer),
        "scope_leaks": sum(bool(e["criteria"].get("scope_leak")) for e in graded),
        "hidden_miss": sum(bool(e["criteria"].get("hidden_miss")) for e in graded),
        "recall_by_category": {
            cat: {
                "retrieval": _mean([float(v) for v in slot["retrieval"] if v is not None]),
                "answer": _mean([float(v) for v in slot["answer"] if v is not None]),
                "scope_leaks": sum(slot["leak"]),
                "hidden_miss": sum(slot["miss"]),
            }
            for cat, slot in by_cat.items()
        },
    }


_DIAGNOSTIC = frozenset({"retrieval_recall", "answer_recall", "hidden_miss", "scope_leak"})


def passed(criteria: Mapping[str, Any]) -> bool:
    """True when no pass/fail criterion is False and no scope leak was recorded (pure).

    Recall legs and ``hidden_miss`` are diagnostics and never fail a case on their own; a ``False``
    ``scope_leak`` means no leak, so it is excluded from the all() check and only a ``True`` fails it.
    """
    return all(v is not False for k, v in criteria.items() if k not in _DIAGNOSTIC) and not (
        criteria.get("scope_leak")
    )


def cited_version_ids(citations: Iterable[Mapping[str, Any]]) -> set[str]:
    """Document version ids of the validated citations, as strings (pure).

    ``_validate_citations`` returns ``Citation.model_dump(by_alias=True)`` dicts. That dump keeps the field alias,
    so the version id is ``document_version_id`` (snake_case), not ``documentVersionId``. Both spellings are read,
    the same as ``modules.chat.citations``; reading only the camelCase key scored every answer's recall as 0.
    """
    ids: set[str] = set()
    for citation in citations:
        value = citation.get("documentVersionId", citation.get("document_version_id"))
        if value is not None:
            ids.add(str(value))
    return ids


def missed_keys(
    gold_keys: Iterable[str], key_map: Mapping[str, str], retrieved: set[str]
) -> list[str]:
    """Sorted gold keys whose resolved version id is absent from ``retrieved`` (pure).

    A key missing from ``key_map`` is ignored, as in ``_grade``, so an unseeded key is never reported as missed.
    """
    return sorted(k for k in set(gold_keys) if k in key_map and key_map[k] not in retrieved)


# --------------------------------------------------------------------------- number matching
_SCALES = (
    (r"nghìn tỷ|ngàn tỷ|trillion", 1e12),
    (r"tỷ|tỉ|billion", 1e9),
    (r"triệu|million", 1e6),
    (r"nghìn|ngàn|thousand", 1e3),
)
_TOKEN = re.compile(
    r"(?<![\d.,])(\d[\d.,]*\d|\d)(?:\s*(nghìn tỷ|ngàn tỷ|trillion|tỷ|tỉ|billion|triệu|million|nghìn|ngàn|thousand))?",
    re.IGNORECASE,
)


def _candidates(token: str) -> list[float]:
    """Every plausible value of a numeric token under en-US (1,234.5) and vi-VN (1.234,5) formats."""
    out: set[float] = set()
    for thousands, decimal in ((",", "."), (".", ",")):
        cleaned = token.replace(thousands, "")
        if cleaned.count(decimal) <= 1:
            try:
                out.add(float(cleaned.replace(decimal, ".")))
            except ValueError:
                pass
    return sorted(out)


def _answer_numbers(text: str) -> list[list[float]]:
    """Candidate values per numeric token of ``text`` (scale words applied as an extra candidate)."""
    text = re.sub(r"\[\d+\]", " ", text)  # citation markers are not figures
    tokens: list[list[float]] = []
    for match in _TOKEN.finditer(text):
        values = _candidates(match.group(1))
        word = (match.group(2) or "").lower()
        if word:
            scale = next(s for pattern, s in _SCALES if re.fullmatch(pattern, word))
            values = [*values, *(v * scale for v in values)]
        tokens.append(values)
    return tokens


def _close(value: float, expected: float) -> bool:
    """Tolerance for model rounding: 0.06 % relative or 0.011 absolute."""
    return abs(value - expected) <= max(0.011, 0.0006 * abs(expected))


def has_number(text: str, expected: float) -> bool:
    """True when some numeric token of ``text`` reads as ``expected`` in either locale format."""
    return any(_close(v, expected) for token in _answer_numbers(text) for v in token)


def forbidden_ok(answer: str, case: Case) -> bool:
    """True when the answer does not assert any of the case's forbidden facts (pure).

    ``forbid_texts`` are strict: any match fails. A forbidden figure is tolerated only when it is named as
    excluded, wrong or superseded: every ``case.nums`` figure must be present in the answer, and every paragraph
    (split on blank lines) that carries the figure must also match ``_EXCLUSION_CUE``. A bare mention with no
    cue, or a figure that is the only result, still fails. Errors: none; never raises on odd text.
    """
    if any(re.search(t, answer, re.IGNORECASE) for t in case.forbid_texts):
        return False
    required_present = all(has_number(answer, n) for n in case.nums)
    paragraphs = _PARAGRAPH_SPLIT.split(answer)
    for forbidden in case.forbid_nums:
        if not has_number(answer, forbidden):
            continue
        # Cue must sit in the same paragraph as the forbidden figure; a correction elsewhere does not excuse it.
        cued = all(_EXCLUSION_CUE.search(p) for p in paragraphs if has_number(p, forbidden))
        if not (required_present and cued):
            return False
    return True
