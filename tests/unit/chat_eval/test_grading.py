from __future__ import annotations

from uuid import uuid4

import pytest

from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.grading import (
    extract_row_ids,
    hidden_miss,
    min_recall_ok,
    recall,
    scope_ids,
    scope_leak,
    summarize_recall,
)


def test_extract_ids_from_docs_and_obs() -> None:
    r = {
        "documents": [{"document_version_id": "v1", "source_id": "s1"}],
        "observations": [{"document_version_id": "v2", "source_id": "s2"}],
    }
    ids = extract_row_ids(r)
    assert ids.version_ids == {"v1", "v2"} and ids.source_ids == {"s1", "s2"}


def test_extract_ids_ignores_rows_without_ids() -> None:
    ids = extract_row_ids({"rows": [{"cnt": 3}], "observation_aggregates": [{"avg": 1.0}]})
    assert ids.version_ids == set() and ids.source_ids == set()


def test_extract_ids_reads_sql_rows_and_stringifies() -> None:
    ids = extract_row_ids({"rows": [{"document_version_id": 7, "source_id": 9}]})
    assert ids.version_ids == {"7"} and ids.source_ids == {"9"}


def test_recall_none_without_gold() -> None:
    assert recall(set(), {"v1"}) is None


def test_recall_fraction() -> None:
    assert recall({"a", "b", "c", "d"}, {"a", "c", "x"}) == 0.5


def test_scope_leak_outside_allowed() -> None:
    assert scope_leak({"s1", "s9"}, {"s1"}, set(), set()) is True
    assert scope_leak({"s1"}, {"s1"}, set(), set()) is False
    assert scope_leak({"s1"}, None, {"vbad"}, {"vbad"}) is True


def test_hidden_miss() -> None:
    assert hidden_miss({"texts_ok": True, "grounded": True}, 0.5, 1.0) is True
    assert hidden_miss({"texts_ok": False}, 0.5, 0.5) is False
    assert hidden_miss({"texts_ok": True}, None, None) is False


def test_summarize_recall_per_category() -> None:
    entries = [
        {
            "category": "latest",
            "criteria": {
                "texts_ok": True,
                "retrieval_recall": 0.5,
                "answer_recall": 1.0,
                "scope_leak": True,
                "hidden_miss": True,
            },
        },
        {
            "category": "latest",
            "criteria": {
                "texts_ok": True,
                "retrieval_recall": None,
                "answer_recall": None,
                "scope_leak": False,
                "hidden_miss": False,
            },
        },
    ]
    out = summarize_recall(entries)
    assert out["retrieval_recall"] == 0.5
    assert out["answer_recall"] == 1.0
    assert out["scope_leaks"] == 1
    assert out["hidden_miss"] == 1
    assert out["recall_by_category"]["latest"]["retrieval"] == 0.5
    assert out["recall_by_category"]["latest"]["scope_leaks"] == 1


def test_scope_keys_to_ids() -> None:
    u = uuid4()
    scoped = Case("x", "cat", "en", "q", source_scope=("fx",))
    assert scope_ids(scoped, {"fx": u}) == [str(u)]
    assert scope_ids(Case("y", "cat", "en", "q"), {"fx": u}) is None
    with pytest.raises(KeyError):
        scope_ids(Case("z", "cat", "en", "q", source_scope=("nope",)), {"fx": u})


def test_min_recall_ok() -> None:
    assert min_recall_ok(None, 0.1) is None
    assert min_recall_ok(0.5, None) is None
    assert min_recall_ok(0.5, 0.4) is False
    assert min_recall_ok(0.5, 0.5) is True
