"""Wiring of the grounding check, the over-budget synthesis round and query-call observability."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core import metrics
from modules.chat._worker import stream, tools
from modules.chat._worker.finalize import _assistant_metadata, _grounding_for
from modules.chat._worker.state import _StreamState
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA
from modules.chat.grounding import UNVERIFIED_SOURCE


def _env(tool_result: str | None, evidence: list[str] | None = None) -> Any:
    messages: list[dict[str, Any]] = []
    if tool_result is not None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "q", "function": {"name": TOOL_QUERY_COLLECTED_DATA}}],
            },
            {"role": "tool", "tool_call_id": "q", "content": tool_result},
        ]
    ctx = SimpleNamespace(
        evidence=[SimpleNamespace(content=c, title="", source_id="s") for c in evidence or []]
    )
    return SimpleNamespace(
        ts=SimpleNamespace(messages_payload=messages),
        turn=SimpleNamespace(
            answer_context=ctx, web_run=None, user_prompt="price?", prior_messages=[]
        ),
    )


def test_plain_turn_is_not_checked() -> None:
    report, widgets = _grounding_for(_env(None), "It is 123,456 units", [], [])
    assert report is None and widgets == []


def test_grounding_flags_answer_widget_and_metadata() -> None:
    env = _env(json.dumps({"rows": [{"v": 100.5}]}))
    table = {"table": {"rows": [["x", "777.7"]], "is_sample": False, "source": "Grounded evidence"}}
    before = metrics.CHAT_UNVERIFIED_NUMBERS._value.get()  # type: ignore[attr-defined]
    report, widgets = _grounding_for(env, "Was 100.5 [1]. Now 999,999 shares.", [{"c": 1}], [table])
    assert report is not None and report.count == 2 and report.partially_uncited
    assert widgets[0]["table"]["source"] == UNVERIFIED_SOURCE
    assert metrics.CHAT_UNVERIFIED_NUMBERS._value.get() == before + 2  # type: ignore[attr-defined]
    meta = _assistant_metadata(_StreamState(), [], None, False, report)["metadata_json"]
    assert meta["unverified_numbers"]["count"] == 2 and meta["partially_uncited"] is True
    assert _assistant_metadata(_StreamState(), [], None, False, None) == {}


def test_evidence_text_verifies_numbers() -> None:
    report, _ = _grounding_for(_env(None, ["Sales were 55,321 last year"]), "Sales: 55,321", [], [])
    assert report is not None and report.count == 0


def _call(name: str, call_id: str) -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": "{}"}


@pytest.mark.asyncio
async def test_widget_round_with_over_budget_query_gets_synthesis_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget, query = _call("render_table", "w"), _call(TOOL_QUERY_COLLECTED_DATA, "q")
    rc = stream._RoundCalls([widget, query], [("p1", widget)], {id(widget): "p1"}, {}, set())
    prior = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": TOOL_QUERY_COLLECTED_DATA}}],
    }
    ts = SimpleNamespace(
        ui_parts=[],
        ui_rounds=0,
        max_parts=10,
        max_ui_rounds=10,
        skill_rounds=0,
        tool_rounds=0,
        synth_done=False,
        stream_iter=None,
        messages_payload=[prior] * stream.MAX_DATA_QUERY_ROUNDS,
    )
    opened: list[Any] = []
    setup = SimpleNamespace(
        ui_tools=None, open_stream=lambda msgs, tools_, tool_choice=None: opened.append(tool_choice)
    )
    env = SimpleNamespace(c=SimpleNamespace(response_id=uuid4()), ts=ts, setup=setup)
    answered: list[bool] = []

    async def fake_flush(*_: Any) -> bool:
        return True

    async def fake_classify(*_: Any) -> Any:
        return rc

    async def fake_answer(*_: Any, budget_exhausted: bool = False) -> None:
        answered.append(budget_exhausted)

    monkeypatch.setattr(stream, "_flush_pending", fake_flush)
    monkeypatch.setattr(stream, "_classify_round", fake_classify)
    monkeypatch.setattr(stream, "_answer_tool_calls", fake_answer)
    rs = SimpleNamespace(acc=SimpleNamespace(calls=lambda: []))
    st = SimpleNamespace(pending_ui={})
    assert await stream._finish_round(env, st, rs) is True  # type: ignore[arg-type]
    assert answered == [True] and opened == ["none"] and ts.synth_done
    assert len(ts.ui_parts) == 1  # the round's widget is kept


@pytest.mark.parametrize(
    ("args", "result", "outcome", "code"),
    [
        ({"mode": "sql", "sql_query": "select 1"}, {"mode": "sql", "rows": [{"a": 1}]}, "ok", None),
        ({}, {"observations": [], "documents": []}, "empty", None),
        ({}, {"status": "no_active_sources_in_scope", "rows": []}, "empty", None),
        (
            {},
            {"error": "invalid_arguments: limit: bad", "rows": []},
            "invalid_arguments",
            "invalid_arguments",
        ),
        ({}, {"error": "query_timeout: narrow", "rows": []}, "query_timeout", "query_timeout"),
        ({}, {"error": "sql_reader_busy", "rows": []}, "sql_reader_busy", "sql_reader_busy"),
        ({}, {"error": "query_execution_failed: X", "rows": []}, "error", "query_execution_failed"),
    ],
)
def test_db_query_summary_outcomes(
    args: dict[str, Any], result: dict[str, Any], outcome: str, code: str | None
) -> None:
    summary = tools._db_query_summary(args, json.dumps(result))
    assert summary["outcome"] == outcome and summary["error_code"] == code


def test_db_query_summary_hides_values_and_hashes_sql() -> None:
    args = {
        "mode": "sql",
        "sql_query": "select secret from t",
        "text_query": "my private text",
        "metrics": ["m"],
    }
    summary = tools._db_query_summary(
        args, json.dumps({"rows": [{"v": "TOPSECRET"}], "truncated": True})
    )
    flat = json.dumps(summary)
    assert "TOPSECRET" not in flat and "select" not in flat and "private" not in flat
    assert len(summary["sql_sha256"]) == 12
    assert summary["filters"] == ["metrics", "text_query"] and summary["truncated"] is True
    assert tools._db_query_summary({}, None)["error_code"] == "exception"


def test_record_db_query_logs_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    import time

    counter = metrics.CHAT_DATA_QUERIES.labels("structured", "ok")
    before = counter._value.get()  # type: ignore[attr-defined]
    with caplog.at_level("INFO", logger="modules.chat.worker"):
        tools._record_db_query(
            uuid4(), {"metrics": ["m"]}, json.dumps({"rows": [1]}), time.monotonic()
        )
    assert counter._value.get() == before + 1  # type: ignore[attr-defined]
    assert "query_collected_data" in caplog.text and "outcome=ok" in caplog.text


def test_prior_answers_and_misattribution_reach_report_and_metadata() -> None:
    """Earlier assistant answers feed the restated check; a wrong-row citation lands in the metadata."""
    rows = json.dumps(
        {
            "rows": [
                {"value": 67500, "citation": "[1]"},
                {"value": 3201.5, "citation": "[2]"},
            ]
        }
    )
    env = _env(rows)
    env.turn.prior_messages = [
        {"role": "user", "content": "rate?"},
        {"role": "assistant", "content": "It is 25,000 VND."},
    ]
    answer = "Eth is 67.500 [2]. I should correct my earlier answer of 25,000 VND."
    report, _ = _grounding_for(env, answer, [{"c": 1}], [])
    assert report is not None and report.count == 0
    assert report.restated == ("25,000",) and report.misattributed == ("67.500",)
    meta = _assistant_metadata(_StreamState(), [], None, False, report)["metadata_json"]
    assert meta["misattributed_numbers"] == ["67.500"] and "unverified_numbers" not in meta
