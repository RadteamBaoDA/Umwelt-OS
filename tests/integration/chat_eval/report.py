"""Per-leg summaries and miss/regression lists for the chat real-model eval report (pure; no I/O).

Each leg gets its own summary, so a single-leg run reports its own cases instead of an empty ``cases: 0``. The
aggregates reuse the grading helpers; every count that names cases also has the ids behind it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tests.integration.chat_eval.grading import passed, summarize_recall
from tests.integration.chat_eval.judge import summarize_judge

_PCT_CRITERIA = (
    "tool_called",
    "numbers_correct",
    "texts_ok",
    "no_forbidden",
    "grounded",
    "citations_valid",
    "empty_ok",
)


def pct(values: list[bool]) -> float | None:
    """Percentage of true values rounded to one decimal; None for an empty list."""
    return round(100 * sum(values) / len(values), 1) if values else None


def miss_list(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Completed entries whose retrieval or answer recall fell short of 1, with their missed gold keys.

    Each item is ``{id, leg, missed_keys, tool_args}``; ``missed_keys`` are the gold keys the retrieval never
    surfaced (see ``missed_keys`` in grading), and ``tool_args`` are the query-tool arguments the model sent.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("status") != "completed":
            continue
        crit = entry["criteria"]
        recalls = (crit.get("retrieval_recall"), crit.get("answer_recall"))
        if any(r is not None and r < 1 for r in recalls):
            out.append(
                {
                    "id": entry["id"],
                    "leg": entry["leg"],
                    "missed_keys": entry.get("missed_keys", []),
                    "tool_args": entry.get("tool_args", []),
                }
            )
    return out


def baseline_regressions(
    entries: Sequence[Mapping[str, Any]], baseline_cases: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Ids that passed in the baseline run and fail in ``entries`` (pure).

    "Fail" means a completed entry that does not pass, or an errored one. Skipped and blocked entries are not
    counted, and an id absent from the baseline is a new case, never a regression.
    """
    was_pass = {
        b["id"]
        for b in baseline_cases
        if b.get("status") == "completed" and passed(b.get("criteria", {}))
    }
    regressed: list[str] = []
    for entry in entries:
        if entry["id"] not in was_pass:
            continue
        if entry.get("status") == "error" or (
            entry.get("status") == "completed" and not passed(entry["criteria"])
        ):
            regressed.append(entry["id"])
    return regressed


def summarize_leg(
    entries: list[dict[str, Any]], baseline_cases: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """The report summary of one leg's entries (pure).

    ``baseline_cases`` (the tool baseline, when given) adds ``baseline_regressions``; without it the list is
    empty. Keys: case counts, the percentage criteria, recall and scope/hidden-miss totals (``scope_leaks`` and
    ``hidden_miss`` are counts, ``scope_leak_ids`` and ``hidden_miss_ids`` the ids), local_only guard outcomes,
    rerank status counts, judge disagreement/uncertain/invalid ids, ``miss_list`` and ``baseline_regressions``.
    """
    done = [e for e in entries if e.get("status") == "completed"]
    crit = {
        k: [bool(e["criteria"][k]) for e in done if e["criteria"][k] is not None]
        for k in _PCT_CRITERIA
    }
    by_cat: dict[str, dict[str, int]] = {}
    for e in done:
        slot = by_cat.setdefault(e["category"], {"pass": 0, "total": 0})
        slot["total"] += 1
        slot["pass"] += int(passed(e["criteria"]))
    blocked = [e for e in entries if e.get("status") == "local_only_blocked"]
    rerank_counts: dict[str, int] = {}
    for e in entries:
        if "rerank_status" in e:
            rerank_counts[e["rerank_status"]] = rerank_counts.get(e["rerank_status"], 0) + 1
    empties = crit["empty_ok"]
    return {
        "cases": len(entries),
        "completed": len(done),
        "errors": [e["id"] for e in entries if e.get("status") == "error"],
        "skipped": [e["id"] for e in entries if e.get("status") == "skipped_deadline"],
        "skipped_no_sql": [e["id"] for e in entries if e.get("status") == "skipped_no_sql"],
        "tool_called_pct": pct(crit["tool_called"]),
        "numbers_correct_pct": pct(crit["numbers_correct"]),
        "texts_ok_pct": pct(crit["texts_ok"]),
        "no_forbidden_pct": pct(crit["no_forbidden"]),
        "grounded_pct": pct(crit["grounded"]),
        "fabricated_numbers_total": sum(len(e["unverified_numbers"]) for e in done),
        "citation_validity_pct": pct(crit["citations_valid"]),
        "empty_case_correct": f"{sum(empties)}/{len(empties)}",
        "mean_query_rounds": round(sum(e["query_rounds"] for e in done) / max(len(done), 1), 2),
        "mean_model_rounds": round(sum(e["model_rounds"] for e in done) / max(len(done), 1), 2),
        "fully_passed": sum(passed(e["criteria"]) for e in done),
        "by_category": by_cat,
        **summarize_recall(done),
        "scope_leak_ids": [e["id"] for e in done if e["criteria"].get("scope_leak")],
        "hidden_miss_ids": [e["id"] for e in done if e["criteria"].get("hidden_miss")],
        "local_only_blocked_ids": [e["id"] for e in blocked],
        "local_only_failed": [e["id"] for e in blocked if not e["passed"]],
        "rerank_status_counts": rerank_counts,
        **summarize_judge(entries),
        "miss_list": miss_list(entries),
        "baseline_regressions": (
            baseline_regressions(entries, baseline_cases) if baseline_cases is not None else []
        ),
    }


def build_summaries(
    entries: Sequence[dict[str, Any]],
    legs: Sequence[str],
    baseline_cases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """One ``summarize_leg`` per leg in ``legs``, keyed by leg name (pure).

    Only the ``tool`` leg is compared with ``baseline_cases``; the baseline is a tool-leg report.
    """
    return {
        leg: summarize_leg(
            [e for e in entries if e["leg"] == leg],
            baseline_cases if leg == "tool" else None,
        )
        for leg in legs
    }
