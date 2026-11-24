from __future__ import annotations

from uuid import uuid4

import pytest

from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.grading import (
    cited_version_ids,
    extract_row_ids,
    hidden_miss,
    min_recall_ok,
    missed_keys,
    recall,
    scope_ids,
    scope_leak,
    summarize_recall,
)
from tests.integration.chat_eval.report import build_summaries


def test_extract_ids_from_docs_and_obs() -> None:
    r = {
        "documents": [{"document_version_id": "v1", "source_id": "s1"}],
        "observations": [{"document_version_id": "v2", "source_id": "s2"}],
    }
    ids = extract_row_ids(r)
    assert ids.version_ids == {"v1", "v2"} and ids.source_ids == {"s1", "s2"}


def test_extract_ids_ignores_rows_without_ids() -> None:
    ids = extract_row_ids({"rows": [{"cnt": 3}], "observation_aggregates": [{"avg": 1.0}]})
    assert ids.version_ids == set() and ids.source_ids == set()


def test_extract_ids_reads_sql_rows_and_stringifies() -> None:
    ids = extract_row_ids({"rows": [{"document_version_id": 7, "source_id": 9}]})
    assert ids.version_ids == {"7"} and ids.source_ids == {"9"}


def test_recall_none_without_gold() -> None:
    assert recall(set(), {"v1"}) is None


def test_recall_fraction() -> None:
    assert recall({"a", "b", "c", "d"}, {"a", "c", "x"}) == 0.5


def test_scope_leak_outside_allowed() -> None:
    assert scope_leak({"s1", "s9"}, {"s1"}, set(), set()) is True
    assert scope_leak({"s1"}, {"s1"}, set(), set()) is False
    assert scope_leak({"s1"}, None, {"vbad"}, {"vbad"}) is True


def test_hidden_miss() -> None:
    assert hidden_miss({"texts_ok": True, "grounded": True}, 0.5, 1.0) is True
    assert hidden_miss({"texts_ok": False}, 0.5, 0.5) is False
    assert hidden_miss({"texts_ok": True}, None, None) is False


def test_summarize_recall_per_category() -> None:
    entries = [
        {
            "category": "latest",
            "criteria": {
                "texts_ok": True,
                "retrieval_recall": 0.5,
                "answer_recall": 1.0,
                "scope_leak": True,
                "hidden_miss": True,
            },
        },
        {
            "category": "latest",
            "criteria": {
                "texts_ok": True,
                "retrieval_recall": None,
                "answer_recall": None,
                "scope_leak": False,
                "hidden_miss": False,
            },
        },
    ]
    out = summarize_recall(entries)
    assert out["retrieval_recall"] == 0.5
    assert out["answer_recall"] == 1.0
    assert out["scope_leaks"] == 1
    assert out["hidden_miss"] == 1
    assert out["recall_by_category"]["latest"]["retrieval"] == 0.5
    assert out["recall_by_category"]["latest"]["scope_leaks"] == 1


def test_scope_keys_to_ids() -> None:
    u = uuid4()
    scoped = Case("x", "cat", "en", "q", source_scope=("fx",))
    assert scope_ids(scoped, {"fx": u}) == [str(u)]
    assert scope_ids(Case("y", "cat", "en", "q"), {"fx": u}) is None
    with pytest.raises(KeyError):
        scope_ids(Case("z", "cat", "en", "q", source_scope=("nope",)), {"fx": u})


def test_min_recall_ok() -> None:
    assert min_recall_ok(None, 0.1) is None
    assert min_recall_ok(0.5, None) is None
    assert min_recall_ok(0.5, 0.4) is False
    assert min_recall_ok(0.5, 0.5) is True


def test_cited_version_ids_reads_both_key_forms() -> None:
    """Cited versions come from the validated citation dicts; ``model_dump(by_alias=True)`` emits snake_case."""
    from modules.chat.schemas import Citation

    version = uuid4()
    real = Citation(
        sourceId=uuid4(),
        documentId=uuid4(),
        documentVersionId=version,
        chunkId=uuid4(),
        title="t",
        quote="q",
    ).model_dump(mode="json", by_alias=True)
    assert "document_version_id" in real and "documentVersionId" not in real
    assert cited_version_ids([real]) == {str(version)}
    assert cited_version_ids([{"documentVersionId": "v1"}, {"sourceType": "web"}]) == {"v1"}


def test_missed_keys_lists_gold_absent_from_retrieval() -> None:
    """A gold key whose version was not retrieved is missed; an unresolved key is ignored."""
    key_map = {"g1": "v1", "g2": "v2"}
    assert missed_keys(("g1", "g2", "unknown"), key_map, {"v1"}) == ["g2"]
    assert missed_keys(("g1",), key_map, {"v1"}) == []


def _crit(**over: object) -> dict[str, object]:
    """A fully populated criteria dict that passes, with ``over`` applied (test helper)."""
    base: dict[str, object] = {
        "tool_called": True,
        "numbers_correct": None,
        "texts_ok": True,
        "no_forbidden": True,
        "grounded": True,
        "citations_valid": True,
        "empty_ok": None,
        "min_recall_ok": None,
        "hidden_miss": False,
        "retrieval_recall": 1.0,
        "answer_recall": 1.0,
        "scope_leak": False,
    }
    return base | over


def test_summary_shape() -> None:
    """Per-leg summary keys, counts and id lists for one tool and one hybrid entry set (pure)."""
    tool_entries = [
        {
            "id": "a",
            "leg": "tool",
            "category": "latest",
            "status": "completed",
            "criteria": _crit(answer_recall=0.5, scope_leak=True),
            "unverified_numbers": [],
            "query_rounds": 1,
            "model_rounds": 2,
            "tool_args": [{"q": "a"}],
            "missed_keys": ["g1"],
        },
        {
            "id": "b",
            "leg": "tool",
            "category": "latest",
            "status": "completed",
            "criteria": _crit(numbers_correct=False, hidden_miss=True, retrieval_recall=0.5),
            "unverified_numbers": [1.0],
            "query_rounds": 1,
            "model_rounds": 2,
            "tool_args": [{"q": "b"}],
            "missed_keys": ["g2"],
            "judge": {"label": "correct_complete", "probs": {}, "status": "ok"},
        },
    ]
    hybrid_entries = [
        {
            "id": "c",
            "leg": "hybrid",
            "category": "paraphrase",
            "status": "completed",
            "criteria": _crit(answer_recall=0.0),
            "unverified_numbers": [],
            "query_rounds": 0,
            "model_rounds": 1,
            "tool_args": [],
            "missed_keys": [],
            "rerank_status": "applied",
            "judge": {"label": "correct_complete", "probs": {}, "status": "uncertain"},
        },
        {
            "id": "d",
            "leg": "hybrid",
            "category": "scope_local_only",
            "status": "local_only_blocked",
            "rerank_status": "applied",
            "passed": True,
        },
        {
            "id": "e",
            "leg": "hybrid",
            "category": "local_only",
            "status": "local_only_blocked",
            "rerank_status": "off",
            "passed": True,
        },
        {
            "id": "f",
            "leg": "hybrid",
            "category": "latest",
            "status": "local_only_blocked",
            "rerank_status": "off",
            "passed": False,
        },
    ]
    baseline = [
        {"id": "a", "status": "completed", "criteria": _crit(scope_leak=True)},
        {"id": "b", "status": "completed", "criteria": _crit()},
    ]
    summaries = build_summaries(
        tool_entries + hybrid_entries, ("tool", "hybrid"), baseline_cases=baseline
    )

    tool, hybrid = summaries["tool"], summaries["hybrid"]
    assert tool["cases"] == 2 and tool["completed"] == 2
    assert tool["scope_leaks"] == 1 and tool["scope_leak_ids"] == ["a"]
    assert tool["hidden_miss"] == 1 and tool["hidden_miss_ids"] == ["b"]
    assert tool["judge_disagreements"] == ["b"]
    assert tool["judge_uncertain"] == [] and tool["judge_invalid"] == []
    assert tool["miss_list"] == [
        {"id": "a", "leg": "tool", "missed_keys": ["g1"], "tool_args": [{"q": "a"}]},
        {"id": "b", "leg": "tool", "missed_keys": ["g2"], "tool_args": [{"q": "b"}]},
    ]
    assert tool["baseline_regressions"] == ["b"]
    assert tool["recall_by_category"]["latest"]["scope_leaks"] == 1

    assert hybrid["cases"] == 4 and hybrid["completed"] == 1
    assert hybrid["judge_uncertain"] == ["c"]
    assert hybrid["local_only_blocked_ids"] == ["d", "e", "f"]
    assert hybrid["local_only_failed"] == ["f"]
    assert hybrid["rerank_status_counts"] == {"applied": 2, "off": 2}
    assert hybrid["baseline_regressions"] == []
    assert hybrid["miss_list"] == [{"id": "c", "leg": "hybrid", "missed_keys": [], "tool_args": []}]
