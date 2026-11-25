"""Known-number sources added for eval findings: evidence titles and row counts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from modules.chat import grounding as g
from modules.chat._worker.finalize import _grounding_for
from modules.chat.schemas import AnswerContext, EvidenceItem


def _item(title: str, content: str, source_id: Any) -> EvidenceItem:
    return EvidenceItem(
        source_id=source_id,
        source_generation=1,
        local_only=False,
        document_id=uuid4(),
        document_version_id=uuid4(),
        version_number=1,
        chunk_id=uuid4(),
        content=content,
        title=title,
        observed_at=datetime.now(UTC),
    )


def _env(evidence: list[EvidenceItem]) -> Any:
    ctx = AnswerContext.model_construct(evidence=evidence)
    turn = SimpleNamespace(
        answer_context=ctx, web_run=None, user_prompt="question", prior_messages=[]
    )
    ts = SimpleNamespace(messages_payload=[])
    return SimpleNamespace(ts=ts, turn=turn)


def test_evidence_title_numbers_are_known() -> None:
    env = _env([_item("CVE-2026-0257 advisory", "Remote code execution flaw.", uuid4())])
    report, _ = _grounding_for(env, "It is CVE-2026-0257 [1].", [{"marker": "[1]"}], [])
    assert report is not None and report.unverified == ()


def test_evidence_count_and_per_source_counts_are_known() -> None:
    a, b = uuid4(), uuid4()
    items = [_item(f"t{i}", "x", a) for i in range(13)] + [_item("u", "x", b)]
    report, _ = _grounding_for(_env(items), "14 items came back, 13 from one source.", [], [])
    assert report is not None and report.unverified == ()


def test_list_lengths_in_tool_results_are_known() -> None:
    result = json.dumps({"documents": [{"title": f"doc {i}"} for i in range(15)]})
    known = g.build_known([result], [], "q")
    ok = g.check_grounding("15 documents matched.", [], known, has_citation=False)
    bad = g.check_grounding("16 documents matched.", [], known, has_citation=False)
    assert ok.unverified == () and len(bad.unverified) == 1


def test_iso_date_parts_in_tool_rows_are_known() -> None:
    result = json.dumps({"rows": [{"listing_date": "2026-05-20"}, {"listing_date": "2026-05-21"}]})
    known = g.build_known([result], [], "q")
    ok = g.check_grounding("Listed on May 20, 21.", [], known, has_citation=False)
    bad = g.check_grounding("Listed on May 20, 23.", [], known, has_citation=False)
    assert ok.unverified == () and bad.unverified == ("23",)


def test_turn_local_date_is_known(monkeypatch: Any) -> None:
    from modules.chat._worker import finalize

    monkeypatch.setattr(finalize, "_today_text", lambda env: "2026-11-25")
    env = _env([_item("t", "x", uuid4())])
    ok, _ = _grounding_for(env, "None between November 12 and 25 [1].", [], [])
    bad, _ = _grounding_for(env, "None between November 12 and 26 [1].", [], [])
    assert ok is not None and ok.unverified == ()
    assert bad is not None and "26" in bad.unverified


def test_complement_counts_are_known_up_to_three_below_a_count() -> None:
    """P7: with 15 rows, "the other 13" (k=2) is grounded; "the other 11" (k=4) is not."""
    known = g.build_known([json.dumps({"rows": [{"label": "x"} for _ in range(15)]})], [], "q")
    assert (
        g.check_grounding("The other 13 were minor.", [], known, has_citation=False).unverified
        == ()
    )
    bad = g.check_grounding("The other 11 were minor.", [], known, has_citation=False)
    assert bad.unverified == ("11",)


def test_non_count_value_does_not_enable_complements() -> None:
    """P7: a 15.0 magnitude is not a count, so 13 stays unverified."""
    known = g.build_known([json.dumps({"rows": [{"magnitude": 15.0}]})], [], "q")
    assert g.check_grounding("About 13.", [], known, has_citation=False).unverified == ("13",)


def test_evidence_counts_enable_complements() -> None:
    """P7: the evidence item total passed as ``counts`` also yields K-1..K-3."""
    known = g.build_known([], ["x"], "q", counts=[15])
    assert g.check_grounding("The other 13.", [], known, has_citation=False).unverified == ()


def test_tool_call_argument_numbers_are_known() -> None:
    """P8: bounds restated from the turn's own query arguments are grounded; others are not."""
    known = g.build_known(
        [json.dumps({"rows": [{"value": 2.1}]})],
        [],
        "q",
        tool_args=['{"min_value":0,"max_value":4.9}'],
    )
    ok = g.check_grounding("Magnitudes from 0.0 to 4.9.", [], known, has_citation=False)
    assert ok.unverified == ()
    bad = g.check_grounding("Magnitudes up to 5.7.", [], known, has_citation=False)
    assert bad.unverified == ("5.7",)
