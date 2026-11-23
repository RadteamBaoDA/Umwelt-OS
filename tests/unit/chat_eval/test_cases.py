from tests.integration.chat_eval.cases import ALL_CASES, CASES, Case


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
