from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.integration.chat_eval import judge as judge_mod
from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.judge import (
    EVIDENCE_ITEM_CHARS,
    EVIDENCE_MAX_ITEMS,
    EVIDENCE_TOTAL_CHARS,
    LABELS,
    NO_EVIDENCE,
    Verdict,
    build_body,
    decisions_url,
    disagreement,
    evidence_block,
    evidence_from_result,
    judge,
    judge_config,
    parse_verdict,
    summarize_judge,
)

ENV = {
    "E2E_JUDGE_BASE_URL": "https://judge.example/v1",
    "E2E_JUDGE_MODEL": "decider-x",
    "E2E_JUDGE_API_KEY": "sk-judge-secret",
}


def _probs(**named: Any) -> dict[str, Any]:
    return {label: named.get(label, 0.0) for label in LABELS}


def test_decisions_url_follows_ragflow_path_rules() -> None:
    assert decisions_url("https://x/v1") == "https://x/alpha/decisions"
    assert decisions_url("https://x/api/v1/") == "https://x/api/alpha/decisions"
    assert decisions_url("https://x/custom") == "https://x/custom/decisions"


def test_build_body_uses_gold_facts_or_no_data_reference() -> None:
    body = build_body("m", "q?", ("fact one", "fact two"), False, "ans")
    assert body["model"] == "m"
    assert body["state"].startswith("Question:")
    assert "fact one" in body["state"] and "ans" in body["state"]
    verdict_q = body["questions"]["verdict"]
    assert verdict_q["type"] == "choice"
    assert set(verdict_q["criteria"]) == set(LABELS)
    empty_body = build_body("m", "q?", (), True, "ans")
    assert "no data exists" in empty_body["state"]


def test_parse_ok_and_uncertain() -> None:
    ok = {
        "answers": {
            "verdict": {"type": "choice", "probabilities": _probs(correct_complete=0.8, wrong=0.2)}
        }
    }
    assert parse_verdict(ok).status == "ok"
    assert parse_verdict(ok).label == "correct_complete"
    close = {
        "answers": {
            "verdict": {
                "type": "choice",
                "probabilities": _probs(correct_complete=0.5, wrong=0.45, fabricated=0.05),
            }
        }
    }
    assert parse_verdict(close).status == "uncertain"


def test_parse_accepts_numeric_strings_like_ragflow() -> None:
    raw = {
        "correct_complete": "0.9",
        "wrong": "0.1",
        "fabricated": "0",
        "correct_partial": "0",
        "correct_no_data": "0",
    }
    verdict = parse_verdict({"answers": {"verdict": {"type": "choice", "probabilities": raw}}})
    assert verdict.status == "ok"


def test_parse_invalid_sum_labels_and_missing_choice() -> None:
    bad_sum = {
        "answers": {
            "verdict": {"type": "choice", "probabilities": {label: 0.3 for label in LABELS}}
        }
    }
    assert parse_verdict(bad_sum).status == "invalid"
    partial = {
        "answers": {"verdict": {"type": "choice", "probabilities": {"correct_complete": 0.5}}}
    }
    assert parse_verdict(partial).status == "invalid"
    assert parse_verdict({}).status == "invalid"
    not_number = {
        "answers": {"verdict": {"type": "choice", "probabilities": _probs(correct_complete="high")}}
    }
    assert parse_verdict(not_number).status == "invalid"


def test_disagreement_rules() -> None:
    passing = {"texts_ok": True, "tool_called": True}
    assert disagreement(passing, Verdict("fabricated", {}, "ok")) is True
    assert disagreement(passing, Verdict("correct_partial", {}, "ok")) is True
    assert disagreement(passing, Verdict("correct_complete", {}, "ok")) is False
    assert disagreement(passing, Verdict("wrong", {}, "uncertain")) is False
    failing = {"texts_ok": False}
    assert disagreement(failing, Verdict("correct_complete", {}, "ok")) is True
    assert disagreement(failing, Verdict("wrong", {}, "ok")) is False


def test_judge_config_needs_all_three_vars() -> None:
    assert judge_config(ENV) is not None
    assert judge_config({k: v for k, v in ENV.items() if k != "E2E_JUDGE_MODEL"}) is None


def test_judge_is_none_without_env() -> None:
    case = Case(id="c", cat="x", lang="en", q="q?", gold_facts=("g",))
    assert asyncio.run(judge(case, "ans", {})) is None


def test_judge_posts_bearer_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        payload = {
            "answers": {
                "verdict": {"type": "choice", "probabilities": _probs(correct_complete=1.0)}
            }
        }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        judge_mod, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    case = Case(id="c", cat="x", lang="en", q="q?", gold_facts=("g",))
    verdict = asyncio.run(judge(case, "ans", ENV))
    assert verdict is not None and verdict.status == "ok"
    assert seen["url"] == "https://judge.example/alpha/decisions"
    assert seen["auth"] == "Bearer sk-judge-secret"


def test_judge_http_error_is_invalid_and_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=json.dumps({"echo": "sk-judge-secret"}))

    monkeypatch.setattr(
        judge_mod, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    case = Case(id="c", cat="x", lang="en", q="q?", gold_facts=("g",))
    verdict = asyncio.run(judge(case, "ans", ENV))
    assert verdict is not None
    assert verdict.status == "invalid"
    assert "sk-judge-secret" not in verdict.error


def test_summarize_judge_lists_ids() -> None:
    entries = [
        {
            "id": "a",
            "status": "completed",
            "criteria": {"texts_ok": True},
            "judge": {"label": "fabricated", "probs": {}, "status": "ok"},
        },
        {
            "id": "b",
            "status": "completed",
            "criteria": {"texts_ok": True},
            "judge": {"label": "wrong", "probs": {}, "status": "uncertain"},
        },
        {
            "id": "c",
            "status": "completed",
            "criteria": {"texts_ok": True},
            "judge": {"label": "", "probs": {}, "status": "invalid"},
        },
        {
            "id": "d",
            "status": "completed",
            "criteria": {"texts_ok": True},
            "judge": {"label": "correct_complete", "probs": {}, "status": "ok"},
        },
    ]
    out = summarize_judge(entries)
    assert out == {
        "judge_disagreements": ["a"],
        "judge_uncertain": ["b"],
        "judge_invalid": ["c"],
    }


def test_build_body_carries_the_evidence_block() -> None:
    body = build_body("m", "q?", ("fact",), False, "ans", ("row one", "row two"))
    assert "Evidence:\n- row one\n- row two" in body["state"]
    assert body["state"].index("Evidence:") < body["state"].index("Answer:")


def test_build_body_without_evidence_says_so() -> None:
    body = build_body("m", "q?", ("fact",), False, "ans")
    assert "(no evidence retrieved)" in body["state"]


def test_evidence_block_bounds_items_and_total() -> None:
    items = [f"item {i} " + "x" * 1000 for i in range(20)]
    block = evidence_block(items)
    lines = block.split("\n")
    assert len(lines) == EVIDENCE_MAX_ITEMS
    assert all(len(line) <= EVIDENCE_ITEM_CHARS + 2 for line in lines)
    assert all(line.endswith("...") for line in lines)
    assert len(block) <= EVIDENCE_TOTAL_CHARS


def test_evidence_block_collapses_whitespace_and_skips_blank_items() -> None:
    assert evidence_block(["  a\n\tb  ", "   "]) == "- a b"


def test_evidence_block_empty_is_explicit() -> None:
    assert evidence_block([]) == NO_EVIDENCE
    assert evidence_block(["  "]) == NO_EVIDENCE


def test_judge_sends_the_evidence_to_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        payload = {
            "answers": {
                "verdict": {"type": "choice", "probabilities": _probs(correct_complete=1.0)}
            }
        }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        judge_mod, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    case = Case(id="c", cat="x", lang="en", q="q?", gold_facts=("g",))
    asyncio.run(judge(case, "ans", ENV, ("retrieved row",)))
    assert "retrieved row" in sent["body"]["state"]


def test_evidence_from_result_splits_rows_into_items() -> None:
    raw = json.dumps({"observations": [{"v": 1}, {"v": 2}], "status": "ok"})
    items = evidence_from_result(raw)
    assert [json.loads(item) for item in items] == [{"v": 1}, {"v": 2}]


def test_evidence_from_result_keeps_unstructured_text_whole() -> None:
    assert evidence_from_result("not json") == ["not json"]
    raw = json.dumps({"error": "boom"})
    assert evidence_from_result(raw) == [raw]
