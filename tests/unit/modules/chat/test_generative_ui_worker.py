"""Model-driven Generative UI in the chat worker: streamed tool calls, fencing, fallback and skill loop."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.model_gateway.client import CapabilityUnsupported
from modules.chat import worker
from modules.chat.generative_ui import finalize_generative_ui
from modules.chat.models import Message, StreamEvent
from modules.chat.schemas import parse_generative_ui
from tests.unit.modules.chat.test_p14_generation_worker import _Gen

SANDBOX_ARGS = {
    "initialHeight": 300, "placeholderMessages": ["Working"], "css": ".a{color:red}",
    "html": "<div class='a'>hi</div>", "jsFunctions": "function f(){}", "jsExpressions": ["f();"],
}


def _text(token: str) -> str:
    """One SSE content chunk."""
    return "data: " + json.dumps({"choices": [{"delta": {"content": token}}]})


def _call(index: int, *, name: str | None = None, args: str = "", call_id: str | None = None) -> str:
    """One SSE tool-call fragment."""
    fn: dict[str, Any] = {"arguments": args}
    if name:
        fn["name"] = name
    frag: dict[str, Any] = {"index": index, "function": fn}
    if call_id:
        frag["id"] = call_id
    return "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [frag]}}]})


def _sandbox_chunks() -> list[str]:
    """Split sandbox arguments so each key lands in its own fragment (one milestone each)."""
    raw = json.dumps(SANDBOX_ARGS)
    cuts = [raw.index('"css"'), raw.index('"html"'), raw.index('"jsFunctions"'), raw.index('"jsExpressions"')]
    parts = [raw[: cuts[0]], raw[cuts[0]: cuts[1]], raw[cuts[1]: cuts[2]], raw[cuts[2]: cuts[3]], raw[cuts[3]:]]
    return [_call(0, name="render_sandboxed_ui", args=parts[0], call_id="c1"), *(_call(0, args=p) for p in parts[1:])]


class _UIGen(_Gen):
    """_Gen whose stream replays one scripted chunk list per model request and records the requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rounds: list[list[str]], prompt: str = "q?") -> None:
        super().__init__(monkeypatch, [], prompt)
        self.rounds = rounds
        self.requests: list[dict[str, Any]] = []
        monkeypatch.setattr(worker, "STREAM_FLUSH_SECONDS", 3600.0)  # only milestones publish

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:
        self.requests.append(kwargs)
        script = self.rounds[min(len(self.requests), len(self.rounds)) - 1]
        for line in script:
            if line == "RAISE_CAPABILITY":
                raise CapabilityUnsupported("no tools")
            yield line
        yield "data: [DONE]"

    def events(self, kind: str) -> list[StreamEvent]:
        return [e for e in self.added if isinstance(e, StreamEvent) and e.event_type == kind]


async def test_tool_stream_publishes_ordered_ui_deltas_then_done_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_text("Here you go."), *_sandbox_chunks()]])
    await gen.run()
    gen.failed.assert_not_awaited()
    assert gen.requests[0]["tools"] and gen.requests[0]["tool_choice"] == "auto"
    assert "LOCALE" in gen.requests[0]["messages"][0]["content"]
    deltas = gen.events("message.ui.delta")
    assert len(deltas) >= 3
    assert all(d.data["status"] == "streaming" and d.data["kind"] == "sandboxed_ui" for d in deltas)
    seqs = [e.seq for e in gen.added if isinstance(e, StreamEvent)]
    assert seqs == sorted(seqs)
    # Snapshots are cumulative: later ones contain every key an earlier one had.
    sizes = [len(d.data["sandbox"]) for d in deltas]
    assert sizes == sorted(sizes) and sizes[-1] >= 4
    (done,) = gen.events("message.ui.done")
    assert done.data["status"] == "completed" and "assembled_html" not in done.data
    assert done.data["sandbox"]["jsExpressions"] == "f();"
    message = next(m for m in gen.added if isinstance(m, Message))
    assert message.metadata_json["generative_ui"] == done.data
    msg_done = gen.events("message.done")[0]
    assert msg_done.data["generative_ui"] == done.data
    assert [e.event_type for e in gen.added if isinstance(e, StreamEvent)][-1] == "message.done"


async def test_invalid_tool_args_produce_no_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_text("Answer."), _call(0, name="render_table", args='{"title": "T"}', call_id="c")]])
    await gen.run()
    gen.failed.assert_not_awaited()
    assert gen.events("message.ui.done") == []
    message = next(m for m in gen.added if isinstance(m, Message))
    assert message.content.endswith("Answer.") and not getattr(message, "metadata_json", None)
    assert "generative_ui" not in gen.events("message.done")[0].data


async def test_first_terminal_wins_and_unknown_tools_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    table = json.dumps({"title": "T", "columns": [{"key": "a", "label": "A"}], "rows": [["1"]]})
    plan = json.dumps({"title": "P", "approach": "x", "steps": [{"title": "s"}]})
    gen = _UIGen(monkeypatch, [[
        _call(0, name="delete_everything", args="{}", call_id="x"),
        _call(1, name="render_table", args=table, call_id="y"),
        _call(2, name="render_plan", args=plan, call_id="z"),
    ]])
    await gen.run()
    (done,) = gen.events("message.ui.done")
    assert done.data["kind"] == "table"


async def test_cancel_mid_tool_stream_publishes_no_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[]])
    cancelled = {"on": False}

    async def stream(**kwargs: Any) -> AsyncIterator[str]:
        for line in _sandbox_chunks():
            yield line
            cancelled["on"] = True  # Stop arrives while the tool arguments are still streaming

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: type("G", (), {"stream": staticmethod(stream)})())
    monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(side_effect=lambda *a, **k: cancelled["on"]))
    marked = AsyncMock()
    monkeypatch.setattr(worker, "_mark_cancelled", marked)
    monkeypatch.setattr(worker, "CANCEL_CHECK_INTERVAL", 0.0)
    await gen.run()
    assert marked.await_count + sum(1 for c in gen.session.commit.await_args_list) > 0
    assert gen.events("message.ui.done") == []
    assert not any(isinstance(m, Message) for m in gen.added)


async def test_capability_unsupported_retries_text_only_and_builds_evidence_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart of the split")
    await gen.run()
    gen.failed.assert_not_awaited()
    assert gen.requests[0].get("tools") and "tools" not in gen.requests[1]
    assert "LOCALE" not in gen.requests[1]["messages"][0]["content"]  # UI prompt only accompanies tools
    (done,) = gen.events("message.ui.done")
    assert done.data["kind"] == "pie_chart" and done.data["sandbox"] is None


async def test_fallback_never_emits_canned_sandbox_for_plain_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Hi")]], prompt="Build a compound interest calculator")
    await gen.run()
    assert gen.events("message.ui.done") == []


async def test_read_skill_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = [_text("."), _call(0, name="read_skill", args='{"name": "master-agent-playbook"}', call_id="s")]
    gen = _UIGen(monkeypatch, [skill])
    await gen.run()
    gen.failed.assert_not_awaited()
    assert len(gen.requests) == 1 + worker.MAX_SKILL_ROUNDS
    last = gen.requests[-1]["messages"]
    assert [m["role"] for m in last[-4:]] == ["assistant", "tool", "assistant", "tool"]
    assert last[-1]["tool_call_id"] == "s" and len(last[-1]["content"]) > 100
    assert gen.events("message.ui.done") == []


async def test_read_skill_then_terminal_tool_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = [_call(0, name="read_skill", args='{"name": "master-agent-playbook"}', call_id="s")]
    gen = _UIGen(monkeypatch, [skill, _sandbox_chunks()])
    await gen.run()
    assert len(gen.requests) == 2
    assert gen.events("message.ui.done")[0].data["kind"] == "sandboxed_ui"


async def test_privacy_redaction_neutralizes_ui_events() -> None:
    statements: list[Any] = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=lambda stmt: statements.append(stmt))
    session.scalar = AsyncMock(return_value=0)
    run = MagicMock(id=uuid4(), status="streaming")
    await worker._privacy_cancel_locked(session, run, 1)
    params = [s.compile().params for s in statements]
    assert any(
        {"message.ui.delta", "message.ui.done"} <= set(next((v for v in p.values() if isinstance(v, list)), []))
        and {} in p.values()
        for p in params
    )


def test_finalize_resolves_citation_numbers_and_drops_unknown() -> None:
    known = {1: {"sourceId": "s1", "documentId": "d1", "chunkId": "c1", "documentVersionId": "v1", "title": "Doc"}}
    raw = json.dumps({
        "title": "T", "columns": [{"key": "a", "label": "A"}], "rows": [["1"], ["2"]], "citations": [1, 9, 1],
    })
    payload = finalize_generative_ui("render_table", raw, known, False)
    assert payload is not None
    assert [c["chunk_id"] for c in payload["table"]["citations"]] == ["c1"]
    assert payload["table"]["records"] == [{"a": "1"}, {"a": "2"}]
    assert payload["sandbox"] is None and payload["status"] == "completed"
    assert finalize_generative_ui("render_table", "{not json", known, False) is None
    assert finalize_generative_ui("read_skill", "{}", known, False) is None


def test_stored_payload_is_validated_on_read() -> None:
    good = {"kind": "table", "status": "completed", "table": {"columns": [], "rows": []}, "assembled_html": "legacy"}
    parsed = parse_generative_ui(good)
    assert parsed is not None and parsed.kind == "table"
    assert parse_generative_ui({"kind": "table", "table": None}) is None
    assert parse_generative_ui({"kind": "sandboxed_ui", "sandbox": {"html": ""}}) is None
    assert parse_generative_ui({"kind": "bogus"}) is None
    assert parse_generative_ui("nope") is None


def test_finalize_chart_keeps_true_chart_type_with_compatible_kind() -> None:
    """Line/scatter/distribution persist as bar_chart but carry the real type for the renderer."""
    for chart_type, kind in (("line", "bar_chart"), ("scatter", "bar_chart"), ("distribution", "bar_chart"), ("pie", "pie_chart")):
        raw = json.dumps({
            "chart_type": chart_type, "title": "T", "x_key": "label",
            "series": [{"key": "value", "label": "V"}], "data": [{"label": "a", "value": 1}],
        })
        payload = finalize_generative_ui("render_chart", raw, {}, False)
        assert payload is not None
        assert payload["kind"] == kind and payload["chart"]["chart_type"] == chart_type


def test_finalize_sandbox_fallback_title_is_localized() -> None:
    """An empty title hint falls back to the owner's locale; unknown locales use English."""
    raw = json.dumps({"html": "<div>x</div>"})
    titles = {
        loc: (finalize_generative_ui("render_sandboxed_ui", raw, {}, False, locale=loc) or {}).get("title")
        for loc in ("en-US", "vi-VN", "fr-FR")
    }
    assert titles["en-US"] == "Interactive view" == titles["fr-FR"]
    assert titles["vi-VN"] == "Chế độ xem tương tác"


async def test_tool_citations_survive_when_prose_has_no_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_table(citations=[1]) keeps citation 1 although the answer text never writes [1]."""
    table = json.dumps({"title": "T", "columns": [{"key": "a", "label": "A"}], "rows": [["1"]], "citations": [1]})
    gen = _UIGen(monkeypatch, [[_text("Plain prose with no marker."), _call(0, name="render_table", args=table, call_id="c")]])
    await gen.run()
    gen.failed.assert_not_awaited()
    (done,) = gen.events("message.ui.done")
    assert done.data["table"]["citations"] and done.data["table"]["citations"][0]["chunk_id"]
    message = next(m for m in gen.added if isinstance(m, Message))
    assert len(message.citations) == 1


async def test_tool_citation_numbers_out_of_range_are_ignored() -> None:
    assert worker._tool_citation_numbers({"arguments": json.dumps({"citations": [0, 1, 2, True, "1", 1]})}, 1) == [1, 1]
    assert worker._tool_citation_numbers({"arguments": "{"}, 3) == [] and worker._tool_citation_numbers(None, 3) == []
