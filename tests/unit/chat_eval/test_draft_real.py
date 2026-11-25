"""Unit checks for the real-data question drafter: validators, parsing, prompt, sampling and CSV."""

from __future__ import annotations

import csv
import io
import math
import sys
from pathlib import Path

import pytest

from tests.integration.chat_eval.draft_real import (
    CSV_COLUMNS,
    Draft,
    Sample,
    aggregate_figures,
    build_prompt,
    extract_numbers,
    find_duplicates,
    is_vietnamese,
    language_reasons,
    number_matches,
    parse_drafts,
    reject_reasons,
    resolve_gold,
    row_aliases,
    seeded_rng,
    stratified_sample,
    strip_question_numbers,
    to_row,
    use_utf8_console,
    write_questions,
)

KEYS = frozenset({"bbc_world/abc-1", "binance/BTCUSDT-7", "open_meteo/hanoi-3"})
NUMBERS = (67500.0, 21.5)


def _draft(**overrides: object) -> Draft:
    """A valid draft over the sample above; ``overrides`` replace single fields."""
    base: dict[str, object] = {
        "cat": "latest_value",
        "lang": "en",
        "q": "What is the latest BTCUSDT price?",
        "gold_keys": (),
        "gold_obs": ("binance/BTCUSDT-7",),
        "gold_facts": ("BTCUSDT latest price is 67500",),
        "nums": (67500.0,),
        "texts": (),
        "empty": False,
        "source_scope": ("binance",),
    }
    base.update(overrides)
    return Draft(**base)  # type: ignore[arg-type]


def test_valid_draft_has_no_reject_reasons() -> None:
    assert reject_reasons(_draft(), KEYS, NUMBERS) == []


def test_gold_key_outside_sample_is_rejected() -> None:
    reasons = reject_reasons(_draft(gold_obs=("binance/UNSEEN-1",)), KEYS, NUMBERS)
    assert any("binance/UNSEEN-1" in r for r in reasons)


def test_number_not_in_rows_is_rejected() -> None:
    reasons = reject_reasons(_draft(nums=(68000.0,)), KEYS, NUMBERS)
    assert any("68000" in r for r in reasons)


def test_number_within_half_percent_is_accepted() -> None:
    # 67800 is 0.44% above 67500: inside the tolerance.
    assert number_matches(67800.0, NUMBERS)
    assert not number_matches(68000.0, NUMBERS)


def test_empty_draft_with_gold_is_rejected() -> None:
    reasons = reject_reasons(_draft(empty=True, nums=()), KEYS, NUMBERS)
    assert any("empty" in r for r in reasons)


def test_empty_draft_without_gold_is_accepted() -> None:
    draft = _draft(empty=True, gold_obs=(), gold_facts=(), nums=())
    assert reject_reasons(draft, KEYS, NUMBERS) == []


def test_unknown_language_is_rejected() -> None:
    assert any("lang" in r for r in reject_reasons(_draft(lang="fr"), KEYS, NUMBERS))


def test_blank_question_is_rejected() -> None:
    assert any("empty question" in r for r in reject_reasons(_draft(q="  "), KEYS, NUMBERS))


def test_duplicate_questions_are_found_case_insensitively() -> None:
    qs = ["What is BTC?", "  what is btc? ", "Hanoi weather?"]
    assert find_duplicates(qs) == {1}


def test_extract_numbers_reads_plain_decimals() -> None:
    assert extract_numbers("Hanoi 21.5 C, pressure 1013, 2026-11-20") == (
        21.5,
        1013.0,
        2026.0,
        11.0,
        20.0,
    )


def test_parse_drafts_reads_fenced_json_array() -> None:
    raw = (
        '```json\n[{"q": "Giá BTC?", "lang": "vi", "cat": "cross_lingual", '
        '"gold_obs": ["binance/BTCUSDT-7"], "gold_keys": [], "gold_facts": ["x"], '
        '"nums": [67500], "texts": [], "empty": false, "source_scope": ["binance"]}]\n```'
    )
    (draft,) = parse_drafts(raw, "cross_lingual")
    assert draft.q == "Giá BTC?"
    assert draft.lang == "vi"
    assert draft.gold_obs == ("binance/BTCUSDT-7",)
    assert draft.nums == (67500.0,)


def test_parse_drafts_rejects_non_array_reply() -> None:
    with pytest.raises(ValueError):
        parse_drafts('{"q": "not a list"}', "latest_value")


def test_parse_drafts_skips_non_object_items() -> None:
    assert parse_drafts('[1, "x"]', "latest_value") == []


def test_build_prompt_states_anchor_and_lists_sample_keys() -> None:
    sample = Sample(
        key="bbc_world/abc-1",
        source_key="bbc_world",
        day="2026-11-20",
        kind="doc",
        text="[bbc_world] Headline | body 21.5",
        numbers=(21.5,),
    )
    messages = build_prompt("latest_value", "2026-11-24 09:00 (Asia/Ho_Chi_Minh)", [sample], 5)
    body = "\n".join(m["content"] for m in messages)
    assert "2026-11-24 09:00 (Asia/Ho_Chi_Minh)" in body
    assert "- R1 :: [bbc_world]" in body
    assert "bbc_world/abc-1" not in body
    assert "5" in body


def test_draft_rows_maps_aliases_back_to_real_keys() -> None:
    import asyncio
    from datetime import UTC, datetime

    from tests.integration.chat_eval.draft_real import draft_rows

    samples = [_obs("binance", 7, 67500.0), _obs("binance", 8, 67000.0)]
    calls = 0

    async def fake_complete(messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return f'[{{"q": "Price {calls}?", "lang": "en", "gold_obs": ["R1"], "nums": [67500]}}]'

    notes: list[str] = []
    anchor = datetime(2026, 11, 24, tzinfo=UTC)
    rows = asyncio.run(draft_rows(samples, 8, anchor, fake_complete, notes))
    # R1 is whichever row the seeded sampler places first, so either real key is correct.
    assert rows, notes
    assert all(row["gold_obs"] in {"binance/7", "binance/8"} for row in rows)
    assert not any(row["gold_obs"].startswith("R") for row in rows)


def _sample(source: str, day: str, idx: int) -> Sample:
    """A minimal sample row for one (source, day) stratum."""
    return Sample(
        key=f"{source}/{idx}", source_key=source, day=day, kind="doc", text="t", numbers=()
    )


def test_stratified_sample_takes_every_stratum_before_repeating() -> None:
    pool = [_sample(s, d, i) for s in ("a", "b") for d in ("d1", "d2", "d3") for i in range(4)]
    picked = stratified_sample(pool, 6, seeded_rng("seed"))
    strata = {(p.source_key, p.day) for p in picked}
    assert len(picked) == 6
    assert len(strata) == 6


def test_stratified_sample_is_deterministic_for_one_seed() -> None:
    pool = [_sample("a", "d1", i) for i in range(10)]
    first = stratified_sample(pool, 4, seeded_rng("2026-11-24"))
    second = stratified_sample(pool, 4, seeded_rng("2026-11-24"))
    assert [p.key for p in first] == [p.key for p in second]


def test_stratified_sample_returns_whole_pool_when_small() -> None:
    pool = [_sample("a", "d1", 0), _sample("b", "d2", 1)]
    assert len(stratified_sample(pool, 10, seeded_rng("x"))) == 2


def test_to_row_joins_multi_values_and_keeps_numbers_round_trippable() -> None:
    draft = _draft(
        gold_keys=("bbc_world/abc-1",),
        gold_obs=(),
        gold_facts=("fact one", "fact two"),
        nums=(67500.0, 21.25),
        texts=("h[aà]",),
        source_scope=("binance", "bbc_world"),
    )
    row = to_row(draft, "r_latest_value_001_en", evidence="E" * 400)
    assert row["gold_keys"] == "bbc_world/abc-1"
    assert row["gold_facts"] == "fact one|fact two"
    assert row["nums"] == "67500;21.25"
    assert row["source_scope"] == "binance;bbc_world"
    assert row["decision"] == ""
    assert row["min_recall"] == ""
    assert row["empty"] == "0"
    assert len(row["evidence_preview"]) == 300


def test_write_questions_uses_utf8_sig_and_fixed_column_order(tmp_path: Path) -> None:
    row = to_row(_draft(q="Giá BTCUSDT mới nhất là bao nhiêu?", lang="vi"), "r_x_001_vi", "")
    out = tmp_path / "questions.csv"
    write_questions(out, [row])
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with out.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        values = next(reader)
    assert tuple(header) == CSV_COLUMNS
    assert values[header.index("q")] == "Giá BTCUSDT mới nhất là bao nhiêu?"


def test_pipe_in_fact_or_regex_is_rejected() -> None:
    reasons = reject_reasons(_draft(gold_facts=("a | b",)), KEYS, NUMBERS)
    assert any("'|'" in r for r in reasons)


def test_unparseable_figure_is_rejected() -> None:
    reasons = reject_reasons(_draft(nums=(float("nan"),)), KEYS, NUMBERS)
    assert any("not a finite number" in r for r in reasons)


def test_infinite_figure_is_rejected_and_never_matches() -> None:
    reasons = reject_reasons(_draft(nums=(float("inf"),)), KEYS, NUMBERS)
    assert any("not a finite number" in r for r in reasons)


def test_parse_drafts_keeps_non_numeric_figures_for_the_validator() -> None:
    # "n/a" and null must not crash the batch; the validator rejects them with a reason.
    raw = '[{"q": "Q?", "lang": "en", "gold_obs": ["R1"], "nums": ["n/a", null, "21.5"]}]'
    (draft,) = parse_drafts(raw, "latest_value")
    assert draft.nums[2] == 21.5
    assert math.isnan(draft.nums[0]) and math.isnan(draft.nums[1])
    assert any("not a finite number" in r for r in reject_reasons(draft, KEYS, NUMBERS))


def test_aliases_resolve_to_real_keys_and_unknown_alias_is_reported() -> None:
    table = row_aliases([_sample("binance", "d1", 7), _sample("bbc_world", "d1", 3)])
    draft = _draft(gold_obs=(" r1 ",), gold_keys=("R9",), gold_facts=())
    resolved, reasons = resolve_gold(draft, table)
    assert resolved.gold_obs == ("binance/7",)
    assert resolved.gold_keys == ("R9",)
    assert any("unknown alias 'R9'" in r for r in reasons)


def test_build_prompt_labels_rows_with_aliases_not_external_ids() -> None:
    sample = Sample(
        key="alternative_me/fng:12345",
        source_key="alternative_me",
        day="2026-11-20",
        kind="obs",
        text="[alternative_me] fng = 21.5 @ 2026-11-20",
        numbers=(21.5,),
    )
    messages = build_prompt("latest_value", "2026-11-24 09:00 (Asia/Ho_Chi_Minh)", [sample], 5)
    body = "\n".join(m["content"] for m in messages)
    assert "- R1 :: [alternative_me]" in body
    assert "12345" not in body
    assert "gold_keys" in body and "R1" in body


def test_aggregate_figures_are_accepted_over_gold_rows() -> None:
    rows = [
        _obs("binance", 3, 4.0),
        _obs("binance", 4, 6.0),
        _obs("binance", 5, 7.0),
    ]
    keys = frozenset(s.key for s in rows)
    draft = _draft(
        gold_keys=(),
        gold_obs=tuple(s.key for s in rows),
        nums=(5.6667, 7.0, 17.0, 3.0),
    )
    numbers = (*(n for s in rows for n in s.numbers), *aggregate_figures(rows))
    assert reject_reasons(draft, keys, numbers) == []


def test_aggregate_figures_reject_unrelated_number() -> None:
    rows = [_obs("binance", 3, 4.0), _obs("binance", 4, 6.0)]
    assert number_matches(5.0, aggregate_figures(rows))
    assert not number_matches(9.0, aggregate_figures(rows))


def test_question_parameter_is_dropped_from_nums_not_rejected() -> None:
    draft = _draft(q="What was the BTCUSDT price over the last 3 days?", nums=(3.0, 67500.0))
    cleaned = strip_question_numbers(draft)
    assert cleaned.nums == (67500.0,)
    assert reject_reasons(cleaned, KEYS, NUMBERS) == []


def test_is_vietnamese_detects_vietnamese_diacritics_only() -> None:
    assert is_vietnamese("Giá BTC mới nhất")
    assert is_vietnamese("Hà Nội")
    assert not is_vietnamese("[binance] BTCUSDT price is 67500")


def test_cross_lingual_must_ask_in_the_opposite_language() -> None:
    en_rows = [_obs("binance", 3, 4.0)]
    vi_rows = [
        Sample(
            key="vn/1",
            source_key="vn",
            day="d1",
            kind="doc",
            text="[vn] Thời tiết Hà Nội",
            numbers=(),
        )
    ]
    # Vietnamese rows are asked about in English; English rows in Vietnamese.
    assert language_reasons(_draft(cat="cross_lingual", lang="en"), vi_rows) == []
    assert language_reasons(_draft(cat="cross_lingual", lang="vi"), en_rows) == []
    assert language_reasons(_draft(cat="cross_lingual", lang="vi"), vi_rows) != []
    assert language_reasons(_draft(cat="cross_lingual", lang="en"), en_rows) != []
    assert language_reasons(_draft(cat="cross_lingual", lang="en"), []) != []
    assert language_reasons(_draft(cat="latest_value", lang="en"), en_rows) == []


def test_console_is_switched_to_utf8_for_vietnamese_output(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    use_utf8_console()
    assert sys.stdout.encoding == "utf-8"
    assert sys.stderr.encoding == "utf-8"


def _obs(source: str, idx: int, value: float) -> Sample:
    """An observation sample with a numeric value, for aggregate checks."""
    return Sample(
        key=f"{source}/{idx}",
        source_key=source,
        day="d1",
        kind="obs",
        text=f"[{source}] v = {value}",
        numbers=(value,),
        value=value,
    )


def test_sample_sql_reads_every_non_local_only_source_not_only_evalc_prefix() -> None:
    from tests.integration.chat_eval.draft_real import DOC_SQL, OBS_SQL

    for sql in (DOC_SQL, OBS_SQL):
        assert "evalc" not in sql
        assert "s.local_only IS FALSE" in sql


def test_unknown_scope_key_rejects_non_empty_draft() -> None:
    reasons = reject_reasons(_draft(source_scope=("cnn",)), KEYS, NUMBERS, frozenset({"binance"}))
    assert any("source_scope key not in snapshot: cnn" in r for r in reasons)


def test_accept_strips_unknown_scope_of_empty_draft() -> None:
    from tests.integration.chat_eval.draft_real import _accept

    draft = _draft(empty=True, gold_obs=(), gold_facts=(), nums=(), source_scope=("cnn", "binance"))
    kept = _accept([draft], [], [], frozenset({"binance"}))
    assert [d.source_scope for d, _ in kept] == [("binance",)]
