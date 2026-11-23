"""Pure recall, scope-leak and hidden-miss grading for the chat real-model eval (no I/O).

Ids are compared as strings: tool JSON carries UUIDs as strings and the validated citations use the same form.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from tests.integration.chat_eval.cases import Case

_ID_KEYS = ("observations", "documents", "rows")


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


def hidden_miss(
    criteria: Mapping[str, Any], retrieval_recall: float | None, answer_recall: float | None
) -> bool:
    """True when no existing criterion failed yet a recall leg fell short of 1.0.

    That is the case the answer passes on its face but missed gold data: it would look fine without the recall
    columns. None recalls (no gold) are ignored.
    """
    if any(value is False for value in criteria.values()):
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
