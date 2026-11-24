import json
import re

from tests.integration.chat_eval.cases import ALL_CASES, CASES, NEW_CASES, Case
from tests.integration.chat_eval.planted import PLANTED_PATH
from tests.integration.chat_eval.seed_all import SEEDED

# Families whose gold lives in planted.json, so every case must name planted keys.
PLANTED_FAMILIES = frozenset(
    {
        "paraphrase",
        "cross_lingual",
        "long_query",
        "stem_en",
        "volume_summary",
        "attr_filter",
        "media_time",
        "injection",
    }
)
REQUIRED_FAMILIES = PLANTED_FAMILIES | {
    "volume_count",
    "scope",
    "scope_local_only",
    "sql_refusal",
}


def _planted_keys() -> set[str]:
    rows = json.loads(PLANTED_PATH.read_text(encoding="utf-8"))["items"]
    return {row["key"] for row in rows}


def test_existing_case_ids_unchanged() -> None:
    assert len(CASES) == 45
    assert CASES[0].id == "btc_latest_en" and CASES[-1].id == "local_calendar_vi"


def test_case_ids_unique() -> None:
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids))


def test_new_fields_default_empty() -> None:
    c = Case("x", "cat", "en", "q")
    assert (c.gold_keys, c.forbid_keys, c.source_scope, c.gold_facts) == ((), (), (), ())
    assert c.min_recall is None and c.needs_sql is False


def test_new_cases_count_and_families() -> None:
    assert len(NEW_CASES) >= 30
    cats = {c.cat for c in NEW_CASES}
    assert REQUIRED_FAMILIES <= cats
    for provider in SEEDED:
        assert f"provider_{provider}" in cats


def test_new_case_keys_resolve_to_planted_items() -> None:
    known = _planted_keys()
    for c in NEW_CASES:
        for key in (*c.gold_keys, *c.forbid_keys):
            assert key in known, f"{c.id}: unknown planted key {key!r}"


def test_new_cases_state_gold_facts() -> None:
    for c in NEW_CASES:
        if c.empty or c.cat == "sql_refusal":
            continue
        assert c.gold_facts, c.id


def test_planted_families_name_gold_keys() -> None:
    for c in NEW_CASES:
        if c.cat in PLANTED_FAMILIES and not c.empty:
            assert c.gold_keys, c.id


def test_injection_cases_forbid_marker() -> None:
    injected = [c for c in NEW_CASES if c.cat == "injection"]
    assert injected
    for c in injected:
        assert "ZX-INJECT-7731" in c.forbid_texts, c.id


def test_scope_cases_pin_source_and_forbid_other_sources() -> None:
    for c in NEW_CASES:
        if c.cat in {"scope", "scope_local_only", "attr_filter", "media_time", "volume_count"}:
            assert c.source_scope, c.id
        if c.cat == "scope":
            assert c.forbid_keys, c.id


def test_sql_cases_need_sql() -> None:
    sql = [c for c in NEW_CASES if c.cat == "sql_refusal"]
    assert sql and all(c.needs_sql for c in sql)


def test_sql_refusal_cases_expect_no_tool() -> None:
    """Refusals are answered without a query tool, so the tool_called criterion must be skipped."""
    sql = [c for c in NEW_CASES if c.cat == "sql_refusal"]
    assert sql and all(c.expects_no_tool for c in sql)


def test_local_calendar_expects_no_tool() -> None:
    """Local-only calendar questions have no collected source, so no tool call is expected."""
    by_id = {c.id: c for c in CASES}
    assert by_id["local_calendar_en"].expects_no_tool is True


def test_provider_fed_press_accepts_either_title_phrase() -> None:
    """The Fed press case accepts "FOMC" or the "Federal Open Market" phrasing."""
    case = next(c for c in NEW_CASES if c.id == "provider_fed_press_en")
    assert any(re.search(t, "Federal Open Market Committee statement") for t in case.texts)
    assert any(re.search(t, "FOMC statement") for t in case.texts)
