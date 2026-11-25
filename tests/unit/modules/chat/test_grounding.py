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


def test_elapsed_durations_are_not_claims() -> None:
    answer = "It is 35 hours old; nothing in the last 90 days, 6 tuần, or 90 ngày. Only 4821 hourly readings."
    assert _unverified(answer, _known({"rows": []})) == ["4821"]


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


def test_absurd_digit_run_does_not_raise_or_yield_infinity() -> None:
    # A 401-digit run overflows float pow and float(); the check must skip it, not crash the turn.
    assert g.extract_numbers("1" + "0" * 400) == []
    nums = g.extract_numbers(f"Total {'9' * 400} and 47 units")
    assert [n.raw for n in nums] == ["47"]
    report = g.check_grounding("Total 47 units", [], g.KnownNumbers(), has_citation=False)
    assert report.unverified == ("47",)


def test_identifiers_timestamps_and_urls_are_not_known_numbers() -> None:
    known = _known(
        {
            "id": "0190f3a2-7c1d-7e00-8a4b-000000000047",
            "observed_at": "2026-11-17T10:30:00Z",
            "value": 2999.5,
        }
    )
    assert _unverified("gold 47 and 30 and 2,999.5", known) == ["47", "30"]


def test_time_and_url_strings_in_other_fields_are_not_known_numbers() -> None:
    known = _known({"note": "updated 10:30:00", "url": "https://example.com/item/47"})
    assert _unverified("it was 30 and item 47", known) == ["30", "47"]


def test_summary_caps_examples() -> None:
    report = g.GroundingReport(tuple(str(1000 + i) for i in range(25)), False, frozenset())
    assert report.summary()["count"] == 25
    assert len(report.summary()["examples"]) == g.MAX_EXAMPLES


@pytest.mark.parametrize(
    "text",
    [
        "As of November 2026 it moved",
        "On 16 November 2026 it moved",
        "On Nov 16, 2026 it moved",
        "Vào ngày 16 tháng 11 năm 2026 nó đổi",
        "Ngày 16/11/2026 nó đổi",
        "Trong tháng 11/2026 nó đổi",
        "Hiện tại là 21:34 ngày 17/11/2026, nên",
        "(21:35 ngày 17/11), từ 14/11 hoặc từ 17/11",
        "No prices for 1–14 November",
        "It reflects only 15 and 16 November",
        "Only the default 90-day window and a 7-day average",
    ],
)
def test_date_forms_leave_no_stray_numbers(text: str) -> None:
    """Month/day/year forms (en and vi) are removed whole, never split into 20 + 26 or 16 + 26."""
    assert g.extract_numbers(text) == []


def test_coordinate_label_makes_both_halves_known() -> None:
    """A "lat,lon" label in tool rows grounds 10.82 and 106.63, also in vi decimal-comma form."""
    known = _known({"rows": [{"label": "10.82,106.63"}, {"label": "21,03,105,85"}]})
    assert _unverified("Lat 10.82 and lon 106.63 and 21,03 and 105,85", known) == []


def test_numbers_inside_urls_are_not_checked() -> None:
    """Digits of a URL in the answer are not data claims."""
    known = _known()
    answer = "See https://example.com/a/98765?id=12345 and www.site.org/4567/89."
    assert _unverified(answer, known) == []


def test_precise_derivations_of_two_data_values_are_reported_as_derived() -> None:
    """A difference / % change of two retrieved prices is 'derived', not unverified; vague ints stay unverified."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known(['{"rows": [{"value": 67123.45}, {"value": 67500}]}'], [], "")
    answer = "BTC rose 376,55 USDT (0,56%) to 67.500 [1]. It is 14 hours old."
    report = check_grounding(answer, [], known, has_citation=True)
    assert set(report.derived) == {"376,55", "0,56%"}
    assert report.unverified == ()  # an elapsed duration is not a data claim
    assert report.summary()["derived"] == 2


def test_made_up_precise_number_is_not_derivable() -> None:
    """A precise figure matching no pair operation stays unverified."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known(['{"rows": [{"value": 67123.45}, {"value": 67500}]}'], [], "")
    report = check_grounding("Change was 412.07 USDT [1].", [], known, has_citation=True)
    assert report.unverified == ("412.07",) and report.derived == ()


_ROWS = (
    '{"rows": [{"symbol": "BTCUSDT", "value": 67500, "citation": "[1]"},'
    ' {"symbol": "ETHUSDT", "value": 3201.5, "citation": "[2]"}]}'
)


def test_number_cited_to_the_wrong_row_is_misattributed() -> None:
    """A value that exists in the data but belongs to another cited row is flagged, not unverified."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known([_ROWS], [], "")
    report = check_grounding("ETHUSDT is 67.500 USDT [2].", [], known, has_citation=True)
    assert report.misattributed == ("67.500",) and report.unverified == ()
    assert report.summary()["misattributed"] == 1


def test_number_cited_to_its_own_row_or_uncited_or_non_row_citation_is_fine() -> None:
    """Right row, several markers, no marker, or a marker without a row (evidence) never misattribute."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known([_ROWS], [], "")
    for answer in (
        "BTCUSDT is 67.500 USDT [1].",
        "BTC 67.500 USDT, ETH 3.201,5 USDT [1][2].",
        "ETHUSDT 67.500 USDT.",
        "ETHUSDT 67.500 USDT [7].",
        "ETHUSDT 3.201,5 USDT [2].",
    ):
        report = check_grounding(answer, [], known, has_citation=True)
        assert report.misattributed == (), answer


def test_prior_answer_number_in_a_correction_is_restated_not_unverified() -> None:
    """A figure from an earlier answer, quoted next to a correction cue, is reported as restated."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known(
        ['{"rows": [{"value": 26312.5}]}'], [], "", prior_answers=["It is 25,000 VND."]
    )
    answer = "Latest is 26,312.5 VND [1]. I need to correct my earlier answer. I said 25,000 VND."
    report = check_grounding(answer, [], known, has_citation=True)
    assert report.restated == ("25,000",) and report.unverified == ()
    assert report.summary()["restated"] == 1


def test_prior_answer_number_without_a_correction_cue_stays_unverified() -> None:
    """Restating an old figure as if current is exactly what the check must keep flagging."""
    from modules.chat.grounding import build_known, check_grounding

    known = build_known(
        ['{"rows": [{"value": 26312.5}]}'], [], "", prior_answers=["It is 25,000 VND."]
    )
    report = check_grounding("The rate today is 25,000 VND.", [], known, has_citation=False)
    assert report.unverified == ("25,000",) and report.restated == ()


@pytest.mark.parametrize(
    "text",
    [
        "Cho 17–24/11 thì không có dữ liệu",  # day/month range prefix
        "Only the window (November 20–24, 2026) matters",  # month-first day range with year
        "Total 19,375 minutes of audio",  # grouped-thousands duration
        "It lasted 13.5 days in total",  # decimal duration
        "Range 2026-11-23..29 was empty",  # ISO date range
    ],
)
def test_date_ranges_and_grouped_durations_leave_no_stray_numbers(text: str) -> None:
    """Ranges and durations are elapsed spans, not claims: no fragment ("17", "24", "19", "13") is left."""
    assert g.extract_numbers(text) == []


def test_real_number_next_to_a_date_is_still_checked() -> None:
    """Skipping date forms must not hide a data value that sits beside them."""
    assert _unverified("price 24 USD on 20/11", _known({"rows": []})) == ["24"]


def test_month_prefix_words_do_not_hide_real_numbers() -> None:
    """Words that merely start like a month ("decreased", "market") keep their numbers checkable."""
    from modules.chat import grounding as g

    assert [n.raw for n in g.extract_numbers("Revenue decreased 12-15 percent.")] == ["12", "15"]
    assert [n.raw for n in g.extract_numbers("The market 30-40 range held.")] == ["30", "40"]
    assert g.extract_numbers("Held on Nov 16, 2026 and 16 November 2026.") == []


def test_digits_inside_identifier_metrics_are_known() -> None:
    """``temperature_2m`` makes "2 m" grounded; an unrelated "3 m" stays unverified."""
    known = _known({"rows": [{"metric": "temperature_2m", "value": 31.4}]})
    assert _unverified("The temperature at 2 m was 31.4 degrees.", known) == []
    assert _unverified("The temperature at 3 m was 31.4 degrees.", known) == ["3 m"]
