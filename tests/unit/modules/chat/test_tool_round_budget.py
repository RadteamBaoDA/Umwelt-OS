"""Unit tests for the continuing-round budgets: query_collected_data rounds vs read_skill rounds."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from modules.chat._worker import base
from modules.chat._worker.stream import _bounded_continue, _RoundCalls
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA

WIDGET = "render_table"
SKILL = "read_skill"


def _call(name: str, call_id: str) -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": "{}"}


def _query_message() -> dict[str, Any]:
    """Transcript entry the answer step appends for a round that carried a query call."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "q", "type": "function", "function": {"name": TOOL_QUERY_COLLECTED_DATA}}
        ],
    }


def _ts(prior_query_rounds: int = 0, skill_rounds: int = 0) -> SimpleNamespace:
    messages: list[dict[str, Any]] = [_query_message() for _ in range(prior_query_rounds)]
    return SimpleNamespace(
        ui_parts=[],
        ui_rounds=0,
        max_parts=10,
        max_ui_rounds=10,
        skill_rounds=skill_rounds,
        messages_payload=messages,
    )


def _round(handled: list[dict[str, str]], widget: dict[str, str] | None = None) -> _RoundCalls:
    round_parts = [("p1", widget)] if widget is not None else []
    part_of = {id(widget): "p1"} if widget is not None else {}
    return _RoundCalls(handled, round_parts, part_of, {}, set())


def test_query_round_is_not_charged_to_skill_budget() -> None:
    ts = _ts(skill_rounds=base.MAX_SKILL_ROUNDS)
    query = _call(TOOL_QUERY_COLLECTED_DATA, "q1")
    assert _bounded_continue(ts, _round([query]), uuid4()) is True
    assert ts.skill_rounds == base.MAX_SKILL_ROUNDS


def test_query_rounds_stop_after_their_own_budget() -> None:
    ts = _ts(prior_query_rounds=base.MAX_DATA_QUERY_ROUNDS)
    query = _call(TOOL_QUERY_COLLECTED_DATA, "q3")
    assert _bounded_continue(ts, _round([query]), uuid4()) is False


def test_read_skill_rounds_do_not_spend_the_query_budget() -> None:
    ts = _ts(prior_query_rounds=base.MAX_DATA_QUERY_ROUNDS)
    assert _bounded_continue(ts, _round([_call(SKILL, "s1")]), uuid4()) is True
    assert ts.skill_rounds == 1


def test_read_skill_budget_still_bounds_skill_rounds() -> None:
    ts = _ts(skill_rounds=base.MAX_SKILL_ROUNDS)
    assert _bounded_continue(ts, _round([_call(SKILL, "s1")]), uuid4()) is False


def test_widget_round_with_query_counts_toward_query_budget() -> None:
    widget = _call(WIDGET, "w1")
    query = _call(TOOL_QUERY_COLLECTED_DATA, "q3")
    ts = _ts(prior_query_rounds=base.MAX_DATA_QUERY_ROUNDS - 1)
    assert _bounded_continue(ts, _round([widget, query], widget), uuid4()) is True
    assert len(ts.ui_parts) == 1

    over = _ts(prior_query_rounds=base.MAX_DATA_QUERY_ROUNDS)
    assert _bounded_continue(over, _round([widget, query], widget), uuid4()) is False
    assert len(over.ui_parts) == 1  # widgets of the stopped round are still recorded
