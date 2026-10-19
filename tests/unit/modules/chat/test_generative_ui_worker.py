"""Model-driven Generative UI in the chat worker: streamed tool calls, fencing, fallback and skill loop."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from core.model_gateway.client import CapabilityUnsupported
from core.model_gateway.schemas import ModelMapping, PrivacySettings
from modules.chat import schemas, worker
from modules.chat.generative_ui import finalize_generative_ui
from modules.chat.generative_ui.external_router import VisualizationRouterError
from modules.chat.models import Message, StreamEvent
from modules.chat.schemas import parse_generative_ui
from modules.chat.user_input import InputOutcome
from modules.settings.public import VizRouterResolved
from tests.unit.modules.chat.test_p14_generation_worker import _Gen
from tests.unit.test_b1c_regressions import _config, _factory

SANDBOX_ARGS = {
    "initialHeight": 300, "placeholderMessages": ["Working"], "css": ".a{color:red}",
    "html": "<div class='a'>hi</div>", "jsFunctions": "function f(){}", "jsExpressions": ["f();"],
}


TABLE_ARGS = json.dumps({"title": "T", "columns": [{"key": "a", "label": "A"}], "rows": [["1"]]})
PLAN_ARGS = json.dumps({"title": "P", "approach": "x", "steps": [{"title": "s"}]})
END: list[str] = []  # a model round that only finishes: no text, no tool calls (ends a widget turn)


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
        # Internal router by default; the fake session's scripted reads must not be spent on settings lookups.
        monkeypatch.setattr(worker.settings_public, "resolve_viz_router", AsyncMock(return_value=VizRouterResolved()))

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:
        self.requests.append(kwargs)
        script = self.rounds[min(len(self.requests), len(self.rounds)) - 1]
        for line in script:
            if line == "RAISE_CAPABILITY":
                raise CapabilityUnsupported("no tools")
            yield line
        yield "data: [DONE]"

    async def run(self, **settings: Any) -> None:  # type: ignore[override]
        """Run with extra worker settings (turn cap, secrets) on top of the minimal fake."""
        self.factory = _factory(self.session)
        await worker.run_response_generation(
            uuid4(), self.factory, SimpleNamespace(ai_allowed_endpoint_cidrs=(), **settings), MagicMock(),  # type: ignore[arg-type]
        )

    def events(self, kind: str) -> list[StreamEvent]:
        return [e for e in self.added if isinstance(e, StreamEvent) and e.event_type == kind]


async def test_tool_stream_publishes_ordered_ui_deltas_then_done_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_text("Here you go."), *_sandbox_chunks()], END])
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


async def test_unknown_tools_are_ignored_and_every_presentation_call_becomes_a_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = _UIGen(monkeypatch, [[
        _call(0, name="delete_everything", args="{}", call_id="x"),
        _call(1, name="render_table", args=TABLE_ARGS, call_id="y"),
        _call(2, name="render_plan", args=PLAN_ARGS, call_id="z"),
    ], END])
    await gen.run()
    done = gen.events("message.ui.done")
    assert [(d.data["part_id"], d.data["kind"]) for d in done] == [("y", "table"), ("z", "plan")]
    assert len(gen.requests) == 2  # parallel calls cost one widget round plus the closing round


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
    gen = _UIGen(monkeypatch, [skill, _sandbox_chunks(), END])
    await gen.run()
    assert len(gen.requests) == 3
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
    gen = _UIGen(monkeypatch, [[_text("Plain prose with no marker."), _call(0, name="render_table", args=table, call_id="c")], END])
    await gen.run()
    gen.failed.assert_not_awaited()
    (done,) = gen.events("message.ui.done")
    assert done.data["table"]["citations"] and done.data["table"]["citations"][0]["chunk_id"]
    message = next(m for m in gen.added if isinstance(m, Message))
    assert len(message.citations) == 1


async def test_tool_citation_numbers_out_of_range_are_ignored() -> None:
    assert worker._tool_citation_numbers({"arguments": json.dumps({"citations": [0, 1, 2, True, "1", 1]})}, 1) == [1, 1]
    assert worker._tool_citation_numbers({"arguments": "{"}, 3) == [] and worker._tool_citation_numbers(None, 3) == []


# ------------------------------------------------------------ multiple widgets per turn


async def test_two_parts_stream_persist_and_complete_with_part_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox streamed in pieces plus a later table: per-part snapshots, two done events, ordered parts."""
    gen = _UIGen(monkeypatch, [[
        _text("Two views."),
        *_sandbox_chunks(),
        _call(1, name="render_table", args=TABLE_ARGS[:20], call_id="c2"),
        _call(1, args=TABLE_ARGS[20:]),
    ], END])
    await gen.run()
    gen.failed.assert_not_awaited()
    deltas = gen.events("message.ui.delta")
    assert {d.data["part_id"] for d in deltas} == {"c1", "c2"}
    assert all(d.data["status"] == "streaming" for d in deltas)
    done = gen.events("message.ui.done")
    assert [d.data["part_id"] for d in done] == ["c1", "c2"]
    message = next(m for m in gen.added if isinstance(m, Message))
    parts = message.metadata_json["generative_ui_parts"]
    assert [p["part_id"] for p in parts] == ["c1", "c2"] and message.metadata_json["generative_ui"] == parts[0]
    assert parts[0]["kind"] == "sandboxed_ui" and parts[1]["kind"] == "table"
    msg_done = gen.events("message.done")[0].data
    assert msg_done["generative_ui"] == parts[0] and msg_done["generative_ui_parts"] == parts
    assert [p.kind for p in schemas.parse_generative_ui_parts(message.metadata_json)] == ["sandboxed_ui", "table"]
    assert [e.event_type for e in gen.added if isinstance(e, StreamEvent)][-1] == "message.done"


async def test_invalid_part_is_dropped_but_siblings_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[
        _call(0, name="render_table", args='{"title": "T"}', call_id="bad"),
        _call(1, name="render_plan", args=PLAN_ARGS, call_id="ok"),
    ], END])
    await gen.run()
    assert [d.data["part_id"] for d in gen.events("message.ui.done")] == ["ok"]


async def test_part_cap_is_enforced_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [_call(i, name="render_table", args=TABLE_ARGS, call_id=f"t{i}") for i in range(5)]
    capped = _UIGen(monkeypatch, [calls])
    await capped.run(generative_ui_max_parts=2)
    assert [d.data["part_id"] for d in capped.events("message.ui.done")] == ["t0", "t1"]
    default = _UIGen(monkeypatch, [calls])
    await default.run()  # default cap is 4
    assert len(default.events("message.ui.done")) == 4


async def test_missing_or_duplicate_call_ids_get_stable_part_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[
        _call(0, name="render_table", args=TABLE_ARGS),
        _call(1, name="render_table", args=TABLE_ARGS, call_id="same"),
        _call(2, name="render_table", args=TABLE_ARGS, call_id="same"),
    ], END])
    await gen.run()
    ids = [d.data["part_id"] for d in gen.events("message.ui.done")]
    assert len(set(ids)) == 3 and ids[0] == "part-1" and ids[1] == "same"


def test_read_path_returns_every_part_and_falls_back_to_legacy_single_widget() -> None:
    table = {"kind": "table", "table": {"rows": []}}
    plan = {"kind": "plan", "plan": {"steps": []}}
    both = schemas.parse_generative_ui_parts({"generative_ui": table, "generative_ui_parts": [table, {"kind": "bogus"}, plan]})
    assert [p.kind for p in both] == ["table", "plan"]
    assert [p.kind for p in schemas.parse_generative_ui_parts({"generative_ui": table})] == ["table"]
    assert schemas.parse_generative_ui_parts(None) == [] and schemas.parse_generative_ui_parts({"x": 1}) == []


# ------------------------------------------------------------------ trip images (non-terminal data tool)


def _media_config(enabled: bool) -> Any:
    """Execution config whose privacy flag allows (or not) remote widget media."""
    return _config(aliases={}, privacy=PrivacySettings(
        allow_remote_embeddings=True, allow_remote_reasoning=True, reasoning_destinations=["omniroute"],
        embedding_destinations=["omniroute"], allow_widget_remote_media=enabled,
    ))


async def test_trip_images_tool_runs_in_loop_then_widget_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    trip_args = json.dumps({"titles": ["Hanoi"]})
    gen = _UIGen(monkeypatch, [
        [_call(0, name="get_trip_stop_images", args=trip_args, call_id="t1"),
         _call(1, name="get_trip_stop_images", args=trip_args, call_id="t2")],  # 2nd call in a round is dropped
        [_call(0, name="render_table", args=TABLE_ARGS, call_id="w")],
        END,
    ])
    monkeypatch.setattr(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_media_config(True)))
    tool = AsyncMock(return_value='{"stops": []}')
    monkeypatch.setattr(worker.media, "run_trip_images_tool", tool)
    await gen.run(csrf_signing_secret=SecretStr("sig"))
    gen.failed.assert_not_awaited()
    names = [t["function"]["name"] for t in gen.requests[0]["tools"]]
    assert "get_trip_stop_images" in names and "REMOTE MEDIA" in gen.requests[0]["messages"][0]["content"]
    tool.assert_awaited_once()
    assert tool.await_args.kwargs["local_only"] is False and tool.await_args.kwargs["secret"] == "sig"
    # The request list is shared and keeps growing: [-4:-2] is the image round, [-2:] the widget round.
    followup = gen.requests[1]["messages"]
    assert [m["role"] for m in followup[-4:-2]] == ["assistant", "tool"]
    assert followup[-4]["tool_calls"][0]["function"]["name"] == "get_trip_stop_images"
    assert followup[-3]["tool_call_id"] == "t1" and followup[-3]["content"] == '{"stops": []}'
    assert json.loads(followup[-1]["content"]) == {"status": "rendered", "part_id": "w"}
    assert [d.data["kind"] for d in gen.events("message.ui.done")] == ["table"]


async def test_trip_images_not_offered_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _UIGen(monkeypatch, [[_text("hi")]])
    monkeypatch.setattr(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_media_config(False)))
    await off.run()
    assert "get_trip_stop_images" not in [t["function"]["name"] for t in off.requests[0]["tools"]]
    assert "REMOTE MEDIA" not in off.requests[0]["messages"][0]["content"]


async def test_trip_images_share_the_extra_round_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _UIGen(monkeypatch, [[_call(0, name="get_trip_stop_images", args='{"titles": ["A"]}', call_id="t")]])
    monkeypatch.setattr(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_media_config(True)))
    monkeypatch.setattr(worker.media, "run_trip_images_tool", AsyncMock(return_value="{}"))
    await loop.run(csrf_signing_secret=SecretStr("sig"))
    assert len(loop.requests) == 1 + worker.MAX_SKILL_ROUNDS  # same extra-round bound as read_skill


# ------------------------------------------------------------------ external visualization router


def _router(monkeypatch: pytest.MonkeyPatch, **resolved: Any) -> AsyncMock:
    """Configure an external router for the turn and return the mocked LLM classifier."""
    monkeypatch.setattr(worker.settings_public, "resolve_viz_router", AsyncMock(
        return_value=VizRouterResolved(**{"kind": "llm", "allow_remote": True, "model": "m", **resolved})))
    monkeypatch.setattr(worker.settings_public, "viz_router_gateway_config",
                        lambda base, r: (_config(aliases={}), ModelMapping(model="m", destination="remote")))
    classify = AsyncMock(return_value="table")
    monkeypatch.setattr(worker.viz, "classify_llm", classify)
    return classify


async def test_external_router_decides_the_no_tools_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # The internal rules would pick a pie chart for this prompt; the external router says table.
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart of the split")
    classify = _router(monkeypatch)
    await gen.run()
    gen.failed.assert_not_awaited()
    classify.assert_awaited_once()
    window = classify.await_args.args[3]
    assert window[-1] == {"role": "user", "content": "Show a pie chart of the split"} and len(window) <= 4
    (done,) = gen.events("message.ui.done")
    assert done.data["kind"] == "table" and done.data["part_id"] == "part-1"


async def test_external_router_failure_fails_the_turn_without_internal_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart of the split")
    classify = _router(monkeypatch)
    classify.side_effect = VisualizationRouterError("timeout")
    await gen.run()
    gen.failed.assert_awaited_once()
    error = gen.failed.await_args.args[4]
    assert isinstance(error, VisualizationRouterError) and error.retryable
    assert gen.events("message.ui.done") == [] and not any(isinstance(m, Message) for m in gen.added)


async def test_external_router_requires_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart")
    classify = _router(monkeypatch, allow_remote=False)
    await gen.run()
    classify.assert_not_awaited()
    assert gen.failed.await_args.args[4].code == "consent_required"


async def test_internal_router_unchanged_when_external_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart of the split")
    monkeypatch.setattr(worker.settings_public, "resolve_viz_router", AsyncMock(return_value=VizRouterResolved()))
    classify = AsyncMock()
    monkeypatch.setattr(worker.viz, "classify_llm", classify)
    await gen.run()
    classify.assert_not_awaited()
    assert gen.events("message.ui.done")[0].data["kind"] == "pie_chart"


async def test_typesafe_router_uses_pinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart of the split")
    monkeypatch.setattr(worker.settings_public, "resolve_viz_router", AsyncMock(
        return_value=VizRouterResolved(kind="typesafe", allow_remote=True, typesafe_key="k")))
    pinned = MagicMock(return_value="transport")
    monkeypatch.setattr(worker, "approved_web_search_transport", pinned)
    classify = AsyncMock(return_value=("bar_chart", {}))
    monkeypatch.setattr(worker.viz, "classify_typesafe", classify)
    await gen.run(ai_allowed_endpoint_hosts=(), web_search_allowed_cidrs=())
    pinned.assert_called_once()
    assert classify.await_args.args[0] == "k" and classify.await_args.args[2] == "transport"
    assert gen.events("message.ui.done")[0].data["kind"] == "bar_chart"


# ------------------------------------------------- widgets across rounds, todos, user input


def _table_round(call_id: str = "t", prefix: str = "") -> list[str]:
    """One model round: optional prose plus a single table widget call."""
    return [*([_text(prefix)] if prefix else []), _call(0, name="render_table", args=TABLE_ARGS, call_id=call_id)]


async def test_widget_text_widget_across_two_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A widget, prose about it, then another widget: unique part ids, text joined, rendered results fed back."""
    gen = _UIGen(monkeypatch, [
        _table_round("t", "First. "),
        [_text("Second. "), _call(0, name="render_plan", args=PLAN_ARGS, call_id="t")],  # provider reused the id
        END,
    ])
    await gen.run()
    gen.failed.assert_not_awaited()
    assert len(gen.requests) == 3
    done = gen.events("message.ui.done")
    ids = [d.data["part_id"] for d in done]
    assert [d.data["kind"] for d in done] == ["table", "plan"] and ids[0] == "t" and len(set(ids)) == 2
    assert {d.data["part_id"] for d in gen.events("message.ui.delta")} <= set(ids)
    message = next(m for m in gen.added if isinstance(m, Message))
    assert "First. Second." in message.content
    assert [p["part_id"] for p in message.metadata_json["generative_ui_parts"]] == ids
    msgs = gen.requests[-1]["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert [json.loads(m["content"]) for m in tool_msgs] == [{"status": "rendered", "part_id": i} for i in ids]
    assert [m["role"] for m in msgs[-4:]] == ["assistant", "tool", "assistant", "tool"]
    assert gen.events("message.done")[0].data["generative_ui_parts"] == message.metadata_json["generative_ui_parts"]


async def test_part_cap_counts_parts_across_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [_table_round("a"), _table_round("b"), _table_round("c"), END])
    await gen.run(generative_ui_max_parts=2)
    assert [d.data["part_id"] for d in gen.events("message.ui.done")] == ["a", "b"]
    assert len(gen.requests) == 2  # the cap stops the loop without another model round


async def test_round_cap_limits_widget_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    capped = _UIGen(monkeypatch, [_table_round("t")])  # the model keeps adding a widget every round
    await capped.run(generative_ui_max_rounds=2, generative_ui_max_parts=8)
    assert len(capped.requests) == 2 and len(capped.events("message.ui.done")) == 2
    default = _UIGen(monkeypatch, [_table_round("t")])
    await default.run(generative_ui_max_parts=8)  # default 3 rounds
    assert len(default.requests) == 3 and len(default.events("message.ui.done")) == 3
    single = _UIGen(monkeypatch, [_table_round("t")])
    await single.run(generative_ui_max_rounds=1)  # 1 keeps the old one-response behaviour
    assert len(single.requests) == 1


async def test_form_part_flows_alongside_other_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    form = json.dumps({"title": "F", "fields": [{"id": "city", "label": "City", "type": "text"}]})
    gen = _UIGen(monkeypatch, [[
        _call(0, name="render_form", args=form, call_id="f"), _call(1, name="render_table", args=TABLE_ARGS, call_id="t"),
    ], END])
    await gen.run()
    assert [(d.data["part_id"], d.data["kind"]) for d in gen.events("message.ui.done")] == [("f", "form"), ("t", "table")]


async def test_local_only_turn_still_fails_the_external_router(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [["RAISE_CAPABILITY"], [_text("Alpha: 40 Beta: 60")]], prompt="Show a pie chart")
    classify = _router(monkeypatch)
    monkeypatch.setattr(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_config(
        aliases={}, privacy=PrivacySettings(allow_remote_reasoning=False),
    )))
    await gen.run()
    classify.assert_not_awaited()
    assert gen.failed.await_args.args[4].code == "local_only_turn"


async def test_todo_tools_are_offered_and_run_inside_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [
        [_call(0, name="manage_todos", args='{"todos": []}', call_id="td")], _table_round("w"), END,
    ])
    tool = AsyncMock(return_value='{"todos": []}')
    monkeypatch.setattr(worker, "run_todo_tool", tool)
    await gen.run()
    gen.failed.assert_not_awaited()
    assert {"manage_todos", "get_todos", "request_user_input"} <= {t["function"]["name"] for t in gen.requests[0]["tools"]}
    assert "TODOS" in gen.requests[0]["messages"][0]["content"]
    tool.assert_awaited_once()
    assert tool.await_args.args[0] == "manage_todos" and tool.await_args.args[1] == '{"todos": []}'
    assert [d.data["kind"] for d in gen.events("message.ui.done")] == ["table"]


def _ask(args: dict[str, Any] | str, call_id: str = "q") -> list[str]:
    """One model round that calls request_user_input."""
    return [_call(0, name="request_user_input", args=args if isinstance(args, str) else json.dumps(args), call_id=call_id)]


async def test_user_input_parks_then_continues_with_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_text("Need one thing. "), *_ask({"kind": "text", "prompt": "Your name?"})], [_text("Thanks.")]])
    create = AsyncMock(return_value={"request_id": "rid"})
    wait = AsyncMock(return_value=InputOutcome("answered", '{"status": "answered", "answer": "Ada"}'))
    monkeypatch.setattr(worker, "create_input_request", create)
    monkeypatch.setattr(worker, "await_user_input", wait)
    await gen.run(chat_input_request_ttl_seconds=60)
    gen.failed.assert_not_awaited()
    create.assert_awaited_once()
    assert create.await_args.kwargs["ttl"].total_seconds() == 60
    assert wait.await_args.args[2] == "rid"
    tool_msg = gen.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "q" and "Ada" in tool_msg["content"]
    message = next(m for m in gen.added if isinstance(m, Message))
    assert message.content.endswith("Thanks.") and gen.events("message.done")


@pytest.mark.parametrize("outcome", ["expired", "cancelled"])
async def test_user_input_expiry_or_cancel_stops_without_terminal_output(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    gen = _UIGen(monkeypatch, [[_text("Partial. "), *_ask({"kind": "text", "prompt": "Name?"})], [_text("never")]])
    monkeypatch.setattr(worker, "create_input_request", AsyncMock(return_value={"request_id": "rid"}))
    monkeypatch.setattr(worker, "await_user_input", AsyncMock(return_value=InputOutcome(outcome, "{}")))  # type: ignore[arg-type]
    await gen.run()
    gen.failed.assert_not_awaited()
    assert len(gen.requests) == 1
    assert not any(isinstance(m, Message) for m in gen.added)
    assert gen.events("message.done") == [] and gen.events("message.citations") == []


async def test_invalid_user_input_args_return_an_error_and_never_park(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [_ask({"kind": "choice", "prompt": "Pick"}), [_text("Ok.")]])
    create = AsyncMock()
    monkeypatch.setattr(worker, "create_input_request", create)
    await gen.run()
    gen.failed.assert_not_awaited()
    create.assert_not_awaited()
    assert "error" in json.loads(gen.requests[1]["messages"][-1]["content"])


async def test_user_input_is_limited_to_two_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [_ask({"kind": "text", "prompt": "Again?"})])  # the model keeps asking
    create = AsyncMock(return_value={"request_id": "rid"})
    monkeypatch.setattr(worker, "create_input_request", create)
    monkeypatch.setattr(worker, "await_user_input", AsyncMock(return_value=InputOutcome("answered", "{}")))
    await gen.run()
    assert create.await_count == worker.MAX_USER_INPUT_REQUESTS_PER_TURN == 2
    assert len(gen.requests) == 3 and gen.events("message.done")
