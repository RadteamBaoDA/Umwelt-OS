"""Numeric grounding check: number parsing, matching and the partially-uncited / widget-label verdicts."""

import json
from typing import Any

import pytest

from modules.chat import grounding as g
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA


def _known(*results: Any, text: str = "", user: str = "") -> g.KnownNumbers:
    """Known numbers from JSON-able tool results plus optional evidence text and user message."""
    return g.build_known([json.dumps(r) for r in results], [text] if text else [], user)


def _unverified(answer: str, known: g.KnownNumbers) -> list[str]:
    return list(g.check_grounding(answer, [], known, has_citation=False).unverified)


@pytest.mark.parametrize(
    ("stated", "stored"),
    [
        ("$64,231.12", {"value": 64231.12}),
        ("64.231,12", {"value": 64231.12}),
        ("64 231,12", {"value": "64231.12"}),
        ("25.400", {"value": 25400}),  # vi thousands
        ("25.400", {"value": 25.4}),  # ambiguous: decimal reading
        ("1,234", {"value": 1234}),
        ("12.5%", {"value": 12.5}),
        ("12.5%", {"value": 0.125}),
        ("65k", {"value": 65000}),
        ("1.2M", {"value": 1234567}),
        ("3,5 triệu", {"value": 3500000}),
        ("2 tỷ", {"value": 2000000000}),
        ("64,231", {"value": 64231.12}),  # rounded to shown digits
        ("65,000", {"value": 64812}),  # rounded to trailing zeros
        ("in doc: 4821 units", None),
    ],
)
def test_formats_and_rounding_match(stated: str, stored: dict[str, Any] | None) -> None:
    known = _known({"rows": [stored]}) if stored else _known(text="stock was 4821 units")
    assert _unverified(f"Result is {stated} today", known) == []


def test_fabricated_number_is_reported() -> None:
    known = _known({"observations": [{"value": 64231.12}]})
    assert _unverified("BTC closed at 71,500.5 and 64,231.12", known) == ["71,500.5"]


def test_number_inside_string_cell_and_user_message_are_known() -> None:
    known = _known(
        {"documents": [{"excerpt": "Revenue grew to 48.7 billion dollars"}]}, user="vs 1,250"
    )
    assert _unverified("Revenue 48.7 billion; you said 1,250", known) == []


def test_citation_bookkeeping_does_not_verify_numbers() -> None:
    known = _known(
        {"rows": [{"citation": "[37]"}], "citation_rule": "use [37] markers", "hint": "limit 50"}
    )
    assert _unverified("The value is 37 and 50 items", known) == ["37", "50"]


def test_non_claims_are_skipped() -> None:
    answer = (
        "1. First item\n2) Second item\n"
        "On 2026-11-17 at 09:30 and 17/11/2026, in 2024, the 3rd quarter [12] had 7 hits.\n"
        "Ngày 5 tháng 12 năm 2026 `x = 98765` and\n```\nprint(123456)\n```"
    )
    assert _unverified(answer, _known({"rows": []})) == []


def test_partially_uncited_needs_a_citation_and_an_uncited_unverified_sentence() -> None:
    known = _known({"rows": [{"v": 100.5}]})
    answer = "Price was 100.5 [1]. Volume rose to 987,654 shares. Next line 55.5 [1]."
    report = g.check_grounding(answer, [], known, has_citation=True)
    assert report.unverified == ("987,654", "55.5")
    assert report.partially_uncited is True
    assert g.check_grounding(answer, [], known, has_citation=False).partially_uncited is False
    cited = "Volume rose to 987,654 shares [1]."
    assert g.check_grounding(cited, [], known, has_citation=True).partially_uncited is False


def _table(rows: list[list[str]], *, sample: bool = False) -> dict[str, Any]:
    return {
        "table": {"rows": rows, "is_sample": sample, "source": "Grounded evidence"},
        "chart": None,
    }


def _chart(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "table": None,
        "chart": {"x_key": "d", "data": data, "is_sample": False, "source": "Grounded evidence"},
    }


def test_widget_with_unmatched_values_is_relabelled_sample_is_not() -> None:
    known = _known({"rows": [{"v": 123.45}]})
    widgets = [
        _table([["BTC", "123.45"]]),
        _table([["BTC", "999.99"]]),
        _table([["BTC", "888.88"]], sample=True),
        _chart([{"d": "2026-11-17", "v": 555.5}]),
        _chart([{"d": "2026-11-17", "v": 123.45}]),
    ]
    report = g.check_grounding("", widgets, known, has_citation=False)
    assert report.bad_widgets == frozenset({1, 3})
    assert report.count == 2
    out = g.relabel_widgets(widgets, report)
    assert out[1]["table"]["source"] == g.UNVERIFIED_SOURCE
    assert out[3]["chart"]["source"] == g.UNVERIFIED_SOURCE
    assert out[0]["table"]["source"] == out[4]["chart"]["source"] == "Grounded evidence"
    assert widgets[1]["table"]["source"] == "Grounded evidence"  # input untouched


def test_query_result_contents_pairs_tool_calls_with_results() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "a", "function": {"name": TOOL_QUERY_COLLECTED_DATA}}],
        },
        {"role": "tool", "tool_call_id": "a", "content": '{"rows": [1]}'},
        {"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": "read_skill"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "skill text 424242"},
    ]
    assert g.query_result_contents(messages) == ['{"rows": [1]}']


def test_summary_caps_examples() -> None:
    report = g.GroundingReport(tuple(str(1000 + i) for i in range(25)), False, frozenset())
    assert report.summary()["count"] == 25
    assert len(report.summary()["examples"]) == g.MAX_EXAMPLES
