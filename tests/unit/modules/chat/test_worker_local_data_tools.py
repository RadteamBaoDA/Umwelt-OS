"""Worker loop wiring for the local-data tools: template tools and ``set_theme`` run inside the tool loop."""

import json

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.chat import ui_theme, worker
from modules.chat.models import StreamEvent
from tests.unit.modules.chat.test_generative_ui_worker import END, _call, _table_round, _UIGen


def _tool_messages(gen: _UIGen) -> list[dict]:
    """Tool-role messages sent in the last model request."""
    return [m for m in gen.requests[-1]["messages"] if m.get("role") == "tool"]


async def test_template_tool_runs_inside_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_call(0, name="list_templates", args="{}", call_id="lt")], _table_round("w"), END])
    runner = AsyncMock(return_value='{"templates": [], "pending_template": null}')
    monkeypatch.setattr(worker, "run_template_tool", runner)
    await gen.run()
    gen.failed.assert_not_awaited()
    runner.assert_awaited_once()
    assert runner.await_args.args[0] == "list_templates"
    names = {t["function"]["name"] for t in gen.requests[0]["tools"]}
    assert {"save_template", "apply_template", "set_theme"} <= names
    (tool_msg,) = _tool_messages(gen)[:1]
    assert tool_msg["tool_call_id"] == "lt" and tool_msg["content"] == '{"templates": [], "pending_template": null}'
    assert [d.data["kind"] for d in gen.events("message.ui.done")] == ["table"]


async def test_set_theme_emits_event_and_seq_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _UIGen(monkeypatch, [[_call(0, name="set_theme", args='{"mode": "dark"}', call_id="th")], _table_round("w"), END])
    real = ui_theme.publish_theme

    async def publish(session, run_id, raw):  # the scripted fake session cannot answer the max(seq) query
        return await real(SimpleNamespace(scalar=AsyncMock(return_value=3), add=session.add), run_id, raw)

    monkeypatch.setattr(worker, "publish_theme", publish)
    await gen.run()
    gen.failed.assert_not_awaited()
    (theme,) = gen.events("ui.theme")
    assert theme.data == {"mode": "dark"} and theme.seq == 4
    tool_msg = next(m for m in _tool_messages(gen) if m["tool_call_id"] == "th")
    assert json.loads(tool_msg["content"])["status"] == "requested"
    # After the theme event the worker resyncs its seq, so every later event keeps increasing.
    later = [e.seq for e in gen.added if isinstance(e, StreamEvent) and e is not theme and e.event_type != "ui.theme"]
    assert later == sorted(set(later))
    assert gen.events("message.done")[0].seq == max(later)
