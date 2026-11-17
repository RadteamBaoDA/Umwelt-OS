"""One whole chat turn driven through ``_stream_turn`` with a scripted model: query_collected_data end to end.

Real: the streaming/tool loop (``_consume_round``, ``_finish_round``, ``_bounded_continue``,
``_answer_tool_calls``), the ``query_collected_data`` handler, the round budgets and the grounding check.
Stubbed: the database query (``execute_db_query_tool``), the fenced flush and status publishing (no DB,
no Redis) and the model (``ScriptedModel`` from tests/integration/fake_model.py; no network).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.chat._worker import stream, tools
from modules.chat._worker.base import MAX_DATA_QUERY_ROUNDS
from modules.chat._worker.finalize import _grounding_for, _validate_citations
from modules.chat._worker.state import _RunCtx, _StreamState
from modules.chat._worker.stream import _query_rounds_used
from modules.chat._worker.tools import _ToolEnv, _TurnState
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA
from modules.chat.schemas import AnswerContext, AnswerContextRequest, EvidenceItem


def _load_fake_model() -> ModuleType:
    """Import tests/integration/fake_model.py by path (the integration directory is not a package)."""
    path = Path(__file__).resolve().parents[3] / "integration" / "fake_model.py"
    spec = importlib.util.spec_from_file_location("fake_model_for_unit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_model = _load_fake_model()
QUERY_ARGS = json.dumps({"mode": "structured", "metrics": ["btc_close"], "limit": 5})
EVIDENCE_TEXT = "BTC daily close 67000.12 USDT, observed 2026-11-16"
BUDGET_EXHAUSTED = json.dumps(
    {
        "error": "tool_budget_exhausted",
        "detail": "The tool budget for this turn is spent; no further tools will run.",
    }
)


class _Session:
    """Async context manager standing in for a DB session; the stubbed query never touches it."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


async def _noop_async(*_: Any, **__: Any) -> None:
    """Stand-in for the status publisher, which would write a stream event."""
    return None


async def _false_async(*_: Any, **__: Any) -> bool:
    """Stand-in for the Redis cancel check: the run is never cancelled."""
    return False


async def _flush_into_accumulated(_c: Any, st: _StreamState) -> bool:
    """Stand-in for the fenced flush: publishes nothing, but moves the buffers into the accumulated text."""
    st.accumulated_text += st.pending
    st.accumulated_reasoning += st.pending_reasoning
    st.drop_pending()
    return True


def _query_result(citation: str) -> dict[str, Any]:
    """Structured query result shaped like ``execute_db_query_tool`` output for one BTC observation."""
    return {
        "mode": "structured",
        "observations": [
            {
                "metric": "btc_close",
                "value": 67000.12,
                "unit": "USDT",
                "observed_at": "2026-11-16T00:00:00+00:00",
                "citation": citation,
            }
        ],
        "aggregates": [],
        "truncated": False,
        "data_notice": None,
        "timezone": "UTC",
    }


@pytest.fixture
def db_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the database query: each call appends one evidence item and returns its ``[n]`` citation."""
    calls: list[dict[str, Any]] = []

    async def fake_db_tool(_session: object, raw: dict[str, Any], **kwargs: Any) -> str:
        calls.append(raw)
        evidence_list: list[EvidenceItem] = kwargs["evidence_list"]
        evidence_list.append(
            EvidenceItem(
                source_id=uuid4(),
                source_generation=1,
                local_only=False,
                document_id=uuid4(),
                document_version_id=uuid4(),
                version_number=1,
                chunk_id=uuid4(),
                content=EVIDENCE_TEXT,
                title="BTC price feed",
                observed_at=datetime(2026, 11, 16, tzinfo=UTC),
            )
        )
        return json.dumps(_query_result(f"[{len(evidence_list)}]"))

    monkeypatch.setattr(tools, "execute_db_query_tool", fake_db_tool)
    monkeypatch.setattr(tools, "_publish_status", _noop_async)
    monkeypatch.setattr(stream, "_publish_status", _noop_async)
    monkeypatch.setattr(stream, "is_run_cancelled", _false_async)
    monkeypatch.setattr(stream, "_flush_pending", _flush_into_accumulated)
    return calls


async def _run_turn(model: Any, prompt: str = "BTC price?") -> SimpleNamespace:
    """Drive one turn through ``_stream_turn``; returns the answer text and the loop state for assertions."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You answer with collected data."},
        {"role": "user", "content": prompt},
    ]
    answer_context = AnswerContext(query=prompt, evidence=[])
    ctx = _RunCtx(
        response_id=uuid4(),
        session_factory=lambda: _Session(),  # type: ignore[arg-type,return-value]
        settings=SimpleNamespace(multi_workspace_enabled=False),  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        conversation_id=uuid4(),
        privacy_fence=None,
        seq=0,
        job_scope=SimpleNamespace(),  # type: ignore[arg-type]
        access_fence=SimpleNamespace(),  # type: ignore[arg-type]
        answer_context=answer_context,
    )
    setup = SimpleNamespace(
        ai_config=SimpleNamespace(gateway_kind="openai_compatible"),
        db_query_on=True,
        trip_images_on=False,
        local_only_turn=False,
        owner_timezone="UTC",
        ui_tools=None,
        open_stream=model.open_stream,
    )
    turn = SimpleNamespace(
        user_prompt=prompt,
        req_params={},
        answer_context=answer_context,
        answer_request=AnswerContextRequest(query=prompt),
        web_run=None,
    )
    ts = _TurnState(
        messages_payload=messages,
        stream_iter=model.open_stream(messages, None),
        tools_active=True,
        max_parts=10,
        max_ui_rounds=10,
        input_ttl_seconds=60,
        last_cancel_check=0.0,
    )
    env = _ToolEnv(c=ctx, claimed=None, turn=turn, setup=setup, ts=ts)  # type: ignore[arg-type]
    st = _StreamState()
    await stream._stream_turn(env, st)
    return SimpleNamespace(env=env, st=st, answer=st.answer_text(), ts=ts)


def _grounding(turn: SimpleNamespace) -> tuple[Any, list[dict[str, Any]], str]:
    """Citation validation then grounding, in the finaliser's order; returns (report, citations, text)."""
    cited = _validate_citations(
        turn.answer,
        turn.env.turn.answer_context,
        turn.env.turn.answer_request,
        None,
        turn.ts.ui_parts,
    )
    report, _ = _grounding_for(turn.env, cited.final_answer, cited.valid_citations, [])
    return report, cited.valid_citations, cited.final_answer


def _tool_contents(model: Any, round_index: int) -> list[str]:
    """Contents of the tool-role messages the model was shown when its ``round_index``-th round opened."""
    messages, _ = model.calls[round_index]
    return [m["content"] for m in messages if m.get("role") == "tool"]


@pytest.mark.asyncio
async def test_query_then_grounded_answer_keeps_valid_citation(
    db_calls: list[dict[str, Any]],
) -> None:
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS),
            fake_model.sse_text_round("BTC was 67,000.12 USDT [1]."),
        ]
    )
    turn = await _run_turn(model)
    report, citations, final = _grounding(turn)

    assert len(db_calls) == 1
    assert final == "BTC was 67,000.12 USDT [1]."
    assert len(citations) == 1 and citations[0]["title"] == "BTC price feed"
    assert report is not None and report.count == 0 and report.partially_uncited is False
    assert _query_rounds_used(turn.ts) == 1
    assert json.loads(_tool_contents(model, 1)[0])["observations"][0]["citation"] == "[1]"


@pytest.mark.asyncio
async def test_fabricated_number_is_flagged_and_text_is_kept(
    db_calls: list[dict[str, Any]],
) -> None:
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS),
            fake_model.sse_text_round("BTC was 70,500 USDT [1]."),
        ]
    )
    turn = await _run_turn(model)
    report, citations, final = _grounding(turn)

    assert final == "BTC was 70,500 USDT [1]."  # the check never rewrites the answer
    assert report is not None and report.count == 1
    # The unmatched figure's sentence carries a [1] marker, so it is not "partially uncited".
    assert report.partially_uncited is False
    assert len(citations) == 1


@pytest.mark.asyncio
async def test_third_query_round_gets_budget_exhausted_then_synthesis(
    db_calls: list[dict[str, Any]],
) -> None:
    assert MAX_DATA_QUERY_ROUNDS == 2  # the script below assumes the budget of two query rounds
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c1"),
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c2"),
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c3"),
            fake_model.sse_text_round("BTC was 67,000.12 USDT [2]."),
        ]
    )
    turn = await _run_turn(model)

    assert len(db_calls) == 2  # the third call never reached the database
    assert model.calls[3][1] == "none"  # closing synthesis round, tools disabled
    assert _tool_contents(model, 3)[-1] == BUDGET_EXHAUSTED
    assert turn.answer == "BTC was 67,000.12 USDT [2]."
    report, citations, _ = _grounding(turn)
    assert report is not None and report.count == 0
    # The synthesis path also appends the stopped round's assistant message, so the transcript count is
    # MAX + 1 here; the loop ends on synth_done, so only the lower bound matters for the budget.
    assert len(citations) == 1 and _query_rounds_used(turn.ts) >= MAX_DATA_QUERY_ROUNDS


@pytest.mark.asyncio
async def test_widget_round_with_over_budget_query_still_synthesises(
    db_calls: list[dict[str, Any]],
) -> None:
    widget_and_query = fake_model.sse_tool_calls_round(
        [
            ("render_table", fake_model.ui_table_arguments(), "w1"),
            (TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c3"),
        ]
    )
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c1"),
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS, "c2"),
            widget_and_query,
            fake_model.sse_text_round("BTC was 67,000.12 USDT [1]."),
        ]
    )
    turn = await _run_turn(model)

    assert len(db_calls) == 2
    assert model.calls[3][1] == "none"
    assert _tool_contents(model, 3)[-1] == BUDGET_EXHAUSTED
    assert len(turn.ts.ui_parts) == 1  # the round's widget is kept
    assert turn.answer == "BTC was 67,000.12 USDT [1]."


@pytest.mark.asyncio
async def test_malformed_arguments_return_error_without_running_query(
    db_calls: list[dict[str, Any]],
) -> None:
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, '{"mode": "structured"'),
            fake_model.sse_text_round("I could not read the data."),
        ]
    )
    turn = await _run_turn(model)

    assert db_calls == []
    assert _tool_contents(model, 1) == [
        json.dumps({"error": "invalid_arguments: malformed JSON", "rows": []})
    ]
    assert turn.answer == "I could not read the data."


@pytest.mark.asyncio
async def test_vietnamese_number_format_is_verified(db_calls: list[dict[str, Any]]) -> None:
    model = fake_model.ScriptedModel(
        [
            fake_model.sse_tool_round(TOOL_QUERY_COLLECTED_DATA, QUERY_ARGS),
            fake_model.sse_text_round("Giá BTC là 67.000,12 USDT [1]."),
        ]
    )
    turn = await _run_turn(model, prompt="Giá BTC hôm nay?")
    report, citations, final = _grounding(turn)

    assert final == "Giá BTC là 67.000,12 USDT [1]."
    assert report is not None and report.count == 0
    assert len(citations) == 1
