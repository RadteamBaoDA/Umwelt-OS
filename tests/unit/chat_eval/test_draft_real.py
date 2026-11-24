"""Unit checks for the real-data question drafter: validators, parsing, prompt, sampling and CSV."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from tests.integration.chat_eval.draft_real import (
    CSV_COLUMNS,
    Draft,
    Sample,
    build_prompt,
    extract_numbers,
    find_duplicates,
    number_matches,
    parse_drafts,
    reject_reasons,
    seeded_rng,
    stratified_sample,
    to_row,
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
    assert "bbc_world/abc-1" in body
    assert "5" in body


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
    assert any("not in sampled rows" in r for r in reasons)


def test_sample_sql_reads_every_non_local_only_source_not_only_evalc_prefix() -> None:
    from tests.integration.chat_eval.draft_real import DOC_SQL, OBS_SQL

    for sql in (DOC_SQL, OBS_SQL):
        assert "evalc" not in sql
        assert "s.local_only IS FALSE" in sql
