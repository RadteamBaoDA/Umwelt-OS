"""Unit tests for the real-data CSV loader and snapshot helpers (no database)."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.cases_real import (
    anchor_from_meta,
    load_real_cases,
    reconcile_scopes,
    split_gold_key,
)
from tests.integration.chat_eval.grading import scope_ids

COLUMNS = (
    "decision,id,cat,lang,q,gold_keys,gold_obs,gold_facts,nums,texts,empty,source_scope,"
    "min_recall,evidence_preview"
).split(",")


def _row(**kw: str) -> dict[str, str]:
    base = dict.fromkeys(COLUMNS, "")
    base |= {"decision": "accept", "id": "r_x_001_en", "cat": "x", "lang": "en", "q": "Q?"}
    base["empty"] = "0"
    return base | kw


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_only_accepted_rows_load(tmp_path: Path) -> None:
    rows = [_row(), _row(id="b", decision="drop"), _row(id="c", decision="")]
    cases = load_real_cases(_write(tmp_path / "q.csv", rows))
    assert [c.id for c in cases] == ["r_x_001_en"]


def test_round_trip_splits_semicolon_and_pipe(tmp_path: Path) -> None:
    row = _row(
        gold_keys="usgs/a;usgs/b",
        gold_facts="fact one|fact two",
        nums="6.1;2500",
        texts="quake|M ?6",
        source_scope="usgs;emsc",
        min_recall="0.5",
        empty="1",
    )
    (case,) = load_real_cases(_write(tmp_path / "q.csv", [row]))
    assert case.gold_keys == ("usgs/a", "usgs/b")
    assert case.gold_facts == ("fact one", "fact two")
    assert case.nums == (6.1, 2500.0)
    assert case.texts == ("quake", "M ?6")
    assert case.source_scope == ("usgs", "emsc")
    assert case.min_recall == 0.5
    assert case.empty is True


def test_blank_min_recall_is_none(tmp_path: Path) -> None:
    (case,) = load_real_cases(_write(tmp_path / "q.csv", [_row()]))
    assert case.min_recall is None
    assert case.gold_keys == ()


def test_bad_nums_names_the_row(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="r_bad_001_en"):
        load_real_cases(_write(tmp_path / "q.csv", [_row(id="r_bad_001_en", nums="1;abc")]))


def test_bad_empty_and_lang_raise(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        load_real_cases(_write(tmp_path / "q.csv", [_row(empty="maybe")]))
    with pytest.raises(ValueError, match="lang"):
        load_real_cases(_write(tmp_path / "q.csv", [_row(lang="fr")]))


def test_split_gold_key_splits_on_first_slash() -> None:
    assert split_gold_key("rss_bbc/https://x.org/a/b") == ("rss_bbc", "https://x.org/a/b")
    with pytest.raises(ValueError):
        split_gold_key("no-slash")


def test_anchor_from_meta(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text(
        json.dumps({"anchor_utc": "2026-11-20T03:04:05+00:00", "tz": "Asia/Ho_Chi_Minh"}),
        encoding="utf-8",
    )
    anchor, tz = anchor_from_meta(tmp_path)
    assert anchor == datetime(2026, 11, 20, 3, 4, 5, tzinfo=UTC)
    assert tz == "Asia/Ho_Chi_Minh"


def _case(cid: str, scope: tuple[str, ...], empty: bool) -> Case:
    return Case(id=cid, cat="x", lang="en", q="q?", empty=empty, source_scope=scope)


def test_reconcile_drops_unknown_keys_of_empty_cases() -> None:
    cases = (_case("a", ("yahoo_finance",), True), _case("b", ("binance", "cnn"), True))
    out = reconcile_scopes(cases, {"binance"})
    assert out[0].source_scope == () and out[1].source_scope == ("binance",)


def test_reconcile_rejects_unknown_key_on_non_empty_case() -> None:
    with pytest.raises(ValueError, match="r_x_001_en.*cnn"):
        reconcile_scopes((_case("r_x_001_en", ("cnn",), False),), {"binance"})


def test_untracked_provider_empty_case_no_longer_raises_key_error() -> None:
    row = _case("r_empty_006_en", ("yahoo_finance",), True)
    (case,) = reconcile_scopes((row,), {"binance"})
    assert scope_ids(case, {"binance": uuid4()}) is None
