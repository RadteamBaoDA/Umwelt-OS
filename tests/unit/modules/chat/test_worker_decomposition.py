"""Characterization tests for the helpers extracted from ``run_response_generation`` and friends."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.chat import worker
from modules.chat.generative_ui.tools.form import _resolve_slots, build_meeting_time_picker_payload
from modules.chat.generative_ui.tools.plan import _first_provenance
from modules.chat.generative_ui.tools.query import query_records
from modules.chat.schemas import MAX_QUOTE_LENGTH, AnswerContext, AnswerContextRequest
from tests.unit.modules.chat._worker_patch import patch_worker
from tests.unit.typing_regressions.test_none_handling_regressions import _evidence


def test_parse_sse_chunk_extracts_delta_fields() -> None:
    payload = json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "content": "hi",
                        "reasoning": "think",
                        "reasoning_details": [{"a": 1}],
                        "tool_calls": [{"index": 0}],
                    }
                }
            ]
        }
    )
    chunk = worker._parse_sse_chunk(payload)
    assert chunk is not None
    assert (chunk.content, chunk.reasoning) == ("hi", "think")
    assert chunk.details == [{"a": 1}] and chunk.tool_fragments == [{"index": 0}]


def test_parse_sse_chunk_prefers_reasoning_content_and_skips_empty_choices() -> None:
    chunk = worker._parse_sse_chunk(
        json.dumps({"choices": [{"delta": {"reasoning_content": "a", "reasoning": "b"}}]})
    )
    assert chunk is not None and chunk.reasoning == "a" and chunk.content == ""
    assert worker._parse_sse_chunk(json.dumps({"choices": []})) is None
    assert worker._parse_sse_chunk(json.dumps({"usage": {}})) is None


def test_parse_sse_chunk_raises_for_malformed_input() -> None:
    with pytest.raises(json.JSONDecodeError):
        worker._parse_sse_chunk("{not json")
    with pytest.raises(AttributeError):
        worker._parse_sse_chunk(json.dumps({"choices": ["str"]}))


def test_validate_citations_keeps_cited_evidence_and_renumbers() -> None:
    first, second = _evidence(content="Alpha fact one."), _evidence(content="Beta fact two.")
    context = AnswerContext(query="q", evidence=[first, second])
    request = AnswerContextRequest(query="q")
    cited = worker._validate_citations("Only the second matters [2].", context, request, None, [])
    assert [c.get("chunk_id") or c.get("chunkId") for c in cited.valid_citations] == [
        str(second.chunk_id)
    ]
    assert cited.number_map == {2: 1}
    assert cited.final_answer == "Only the second matters [1]."


def test_validate_citations_web_only_answer_gets_web_citation() -> None:
    hit = SimpleNamespace(url="https://example.test/a", title="A", snippet="snippet")
    web_run = worker._WebSearchRun({"status": "used"}, (hit,), "searxng")  # type: ignore[arg-type]
    context = AnswerContext(query="q", evidence=[])
    cited = worker._validate_citations(
        "Per the web [1].", context, AnswerContextRequest(query="q"), web_run, []
    )
    assert (
        len(cited.valid_citations) == 1
        and cited.valid_citations[0]["url"] == "https://example.test/a"
    )
    assert cited.number_map == {1: 1}


def test_validate_citations_truncates_long_web_snippet_to_quote_limit() -> None:
    """Page-text providers return snippets up to SNIPPET_MAX (4000); the stored quote is capped, not rejected."""
    hit = SimpleNamespace(url="https://example.test/a", title="A", snippet="x" * 1500)
    web_run = worker._WebSearchRun({"status": "used"}, (hit,), "searxng")  # type: ignore[arg-type]
    context = AnswerContext(query="q", evidence=[])
    cited = worker._validate_citations(
        "Per the web [1].", context, AnswerContextRequest(query="q"), web_run, []
    )
    assert len(cited.valid_citations) == 1
    assert len(cited.valid_citations[0]["quote"]) == MAX_QUOTE_LENGTH
    assert cited.final_answer == "Per the web [1]."


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()


async def _fence(
    monkeypatch: pytest.MonkeyPatch, *, cancelled: bool, evidence_ok: bool, with_evidence: bool
) -> tuple[Any, list[str]]:
    calls: list[str] = []
    run = SimpleNamespace(status="streaming")
    patch_worker(monkeypatch, "_lock_live_response", AsyncMock(return_value=(True, run)))
    patch_worker(monkeypatch, "is_run_cancelled", AsyncMock(return_value=cancelled))
    patch_worker(
        monkeypatch,
        "_cancel_response_locked",
        AsyncMock(side_effect=lambda *_a: calls.append("cancel")),
    )
    patch_worker(
        monkeypatch,
        "_privacy_cancel_locked",
        AsyncMock(side_effect=lambda *_a: calls.append("redact")),
    )
    patch_worker(monkeypatch, "revalidate_context_fence", AsyncMock(return_value=(evidence_ok, [])))
    c = worker._RunCtx(
        response_id=uuid4(),
        session_factory=None,
        settings=None,
        redis=None,  # type: ignore[arg-type]
        conversation_id=uuid4(),
        privacy_fence={},
        seq=3,
        job_scope=SimpleNamespace(),  # type: ignore[arg-type]
    )
    if with_evidence:
        c.answer_context = AnswerContext(query="q", evidence=[_evidence()])
    session = _Session()
    outcome, _run = await worker._fence_live_run(session, c, check_evidence=True)  # type: ignore[arg-type]
    if outcome is not worker._FenceOutcome.LIVE:
        session.commit.assert_awaited_once()
    return outcome, calls


async def test_fence_live_run_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _fence(monkeypatch, cancelled=False, evidence_ok=True, with_evidence=True) == (
        worker._FenceOutcome.LIVE,
        [],
    )
    assert await _fence(monkeypatch, cancelled=True, evidence_ok=True, with_evidence=True) == (
        worker._FenceOutcome.CANCELLED,
        ["cancel"],
    )
    assert await _fence(monkeypatch, cancelled=False, evidence_ok=False, with_evidence=True) == (
        worker._FenceOutcome.REDACTED,
        ["redact"],
    )
    # No retrieved evidence: nothing to revalidate, so a failing fence is never consulted.
    assert await _fence(monkeypatch, cancelled=False, evidence_ok=False, with_evidence=False) == (
        worker._FenceOutcome.LIVE,
        [],
    )


def test_tool_registry_covers_every_data_tool_and_accepts_new_handlers() -> None:
    names = {
        worker.TOOL_READ_SKILL,
        worker.TOOL_TRIP_IMAGES,
        worker.TOOL_QUERY_COLLECTED_DATA,
        worker.TOOL_REQUEST_USER_INPUT,
        *worker.LOCAL_DATA_TOOLS,
    }
    assert names <= set(worker._TOOL_HANDLERS)
    # All local data tools share one handler instance, hence one per-round cap.
    assert len({id(worker._TOOL_HANDLERS[n]) for n in worker.LOCAL_DATA_TOOLS}) == 1
    assert worker._TOOL_HANDLERS[worker.TOOL_REQUEST_USER_INPUT].data_round is False

    class Custom:
        data_round = True

        def available(self, env: Any) -> bool:
            return True

        def round_cap(self, env: Any) -> int:
            return 1

        async def run(self, env: Any, call: dict[str, str]) -> str:
            return "ok"

    worker.register_chat_tool(["x_custom_tool"], Custom())
    try:
        assert "x_custom_tool" in worker._TOOL_HANDLERS
    finally:
        worker._TOOL_HANDLERS.pop("x_custom_tool")


def test_request_user_input_cap_shrinks_with_answered_requests() -> None:
    handler = worker._TOOL_HANDLERS[worker.TOOL_REQUEST_USER_INPUT]
    env = SimpleNamespace(ts=SimpleNamespace(input_requests=0))
    assert handler.round_cap(env) == worker.MAX_USER_INPUT_REQUESTS_PER_TURN  # type: ignore[arg-type]
    env.ts.input_requests = worker.MAX_USER_INPUT_REQUESTS_PER_TURN
    assert handler.round_cap(env) == 0  # type: ignore[arg-type]


def test_round_terminals_prefixes_colliding_part_ids() -> None:
    class Acc:
        def terminals(self, limit: int) -> list[tuple[str, dict[str, str]]]:
            return [("part-1", {"name": "render_table", "arguments": "{}"})][:limit]

    ts = SimpleNamespace(ui_parts=[("part-1", {})], ui_rounds=1, max_parts=4)
    out = worker._round_terminals(ts, Acc())  # type: ignore[arg-type]
    assert [pid for pid, _ in out] == ["r2-part-1"]


def test_stream_state_drop_pending_clears_only_unpublished_buffers() -> None:
    st = worker._StreamState(
        pending="a", pending_reasoning="b", pending_ui={"p": {}}, accumulated_text="kept"
    )
    st.drop_pending()
    assert (st.pending, st.pending_reasoning, st.pending_ui, st.accumulated_text) == (
        "",
        "",
        {},
        "kept",
    )


def test_query_records_filter_group_sort_limit() -> None:
    rows = [{"k": "A", "v": "1,000"}, {"k": "a", "v": 5}, {"k": "B", "v": 2}, {"k": "C", "v": "x"}]
    assert query_records(rows, filters={"k": ["A", "a"]}, limit=1) == [{"k": "A", "v": "1,000"}]
    grouped = query_records(rows, group_by="k", metric="v", agg="sum", sort_by="v")
    assert [(r["k"], r["v"]) for r in grouped] == [
        ("A", 1000.0),
        ("a", 5.0),
        ("B", 2.0),
        ("C", 0.0),
    ]
    assert query_records(rows, group_by="k", agg="avg")[0] == {"k": "A", "count": 1}
    assert query_records(rows, filters={"k": "b"}) == [{"k": "B", "v": 2}]
    assert (
        query_records(rows, group_by="k", metric="v", agg="max", sort_by="v", descending=False)[0][
            "v"
        ]
        == 0.0
    )


def test_meeting_slots_defaults_and_caller_slots() -> None:
    assert [s.id for s in _resolve_slots(None, "UTC")] == ["slot-1", "slot-2", "slot-3"]
    custom = _resolve_slots([{"label": "L"}, {"id": "z", "recommended": True}], "Asia/Tokyo")
    assert [(s.id, s.label, s.timezone, s.recommended) for s in custom] == [
        ("slot-1", "L", "Asia/Tokyo", True),
        ("z", "Option 2", "Asia/Tokyo", True),
    ]
    assert build_meeting_time_picker_payload().selected_slot_id == "slot-1"


def test_first_provenance_reads_dicts_objects_and_empty() -> None:
    assert _first_provenance([]) == (None, None, None)
    assert _first_provenance([{"source_id": "s", "document_id": "", "chunk_id": "c"}]) == (
        "s",
        None,
        "c",
    )
    assert _first_provenance([SimpleNamespace(source_id="s", document_id="d")]) == ("s", "d", None)


def test_validate_citations_web_first_then_tool_evidence_numbering() -> None:
    """Prompt web [1] and a DB row fetched later as [2] resolve to the web hit and the evidence item."""
    hit = SimpleNamespace(url="https://example.test/a", title="A", snippet="snippet")
    web_run = worker._WebSearchRun(  # type: ignore[arg-type]
        {"status": "used"}, (hit,), "searxng", evidence_base=0
    )
    db_item = _evidence(content="Quake near Crete.")
    context = AnswerContext(query="q", evidence=[db_item])
    cited = worker._validate_citations(
        "Web says A [1]. The database says quake [2].",
        context,
        AnswerContextRequest(query="q"),
        web_run,
        [],
    )
    assert len(cited.valid_citations) == 2
    assert any(c.get("url") == "https://example.test/a" for c in cited.valid_citations)
    assert any(
        str(c.get("chunkId") or c.get("chunk_id")) == str(db_item.chunk_id)
        for c in cited.valid_citations
    )
    # Model numbers: [1] web, [2] evidence; both survive in the map.
    assert set(cited.number_map) == {1, 2}


def test_citation_number_shifts_only_tool_evidence_behind_web_results() -> None:
    """``_citation_number`` keeps prompt-time evidence numbers and shifts later items past the web block."""
    from modules.chat.db_query import _citation_number

    assert _citation_number(0, None) == 1
    assert _citation_number(0, (0, 1)) == 2
    assert _citation_number(1, (2, 3)) == 2
    assert _citation_number(2, (2, 3)) == 6
