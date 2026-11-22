"""Every brief fact kind survives capture -> group validation -> citation validation -> eligibility."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.dashboard import _brief_evidence as ev
from modules.dashboard import briefs, context
from modules.dashboard.daily_schemas import ALL_BRIEF_SECTIONS
from modules.dashboard.models import DailyBriefEvidence

BRIEF_ID = uuid4()
SOURCE = str(uuid4())
KINDS_BY_SECTION = {
    "stories": ["stories"],
    "markets": ["markets"],
    "weather": ["weather"],
    "hazards": ["hazards"],
    "agenda": ["tasks", "goals", "events"],
    "source_health": ["source_health"],
}


def _fact(kind: str) -> dict[str, Any]:
    """One prompted fact of ``kind`` shaped like ``briefs._facts`` output."""
    marker = kind in ev.MARKER_FACT_KINDS
    if kind in ("tasks", "goals") or marker:
        status, supports = "independent", []
    else:  # document-backed kinds
        status = "supported"
        supports = [
            {
                "document_id": str(uuid4()),
                "document_version_id": str(uuid4()),
                "chunk_id": str(uuid4()),
                "source_id": SOURCE,
            }
        ]
    if marker and kind != "source_health":
        fact_id = context._obs_id(SimpleNamespace(source_id=SOURCE, metric="m", symbol="S"))
    else:
        fact_id = SOURCE if marker else str(uuid4())
    return {
        "kind": kind,
        "id": fact_id,
        "title": "t",
        "detail": "d",
        "source_ids": [SOURCE] if (marker or supports) else [],
        "_fact_hash": "a" * 64,
        "_lineage_status": status,
        "_supports": supports,
    }


def _grouped(facts: list[dict[str, Any]]) -> dict[int, list[DailyBriefEvidence]]:
    return {
        ref: briefs._evidence_rows_for_fact(BRIEF_ID, ref, fact)
        for ref, fact in enumerate(facts, start=1)
    }


def _all_kinds() -> list[str]:
    kinds = [kind for section in ALL_BRIEF_SECTIONS for kind in KINDS_BY_SECTION[section]]
    assert len(kinds) == 8
    return kinds


def test_round_trip_for_every_kind() -> None:
    facts = [_fact(kind) for kind in _all_kinds()]
    grouped = _grouped(facts)
    sources: set[Any] = set()
    for rows in grouped.values():
        assert ev._validate_fact_group(rows, set(), sources, set())
    citations = [
        {"ref": ref, **{k: fact[k] for k in ("kind", "id", "title", "source_ids")}}
        for ref, fact in enumerate(facts, start=1)
    ]
    by_ref = ev._validate_citations(SimpleNamespace(citations=citations), grouped)  # type: ignore[arg-type]
    assert by_ref is not None and set(by_ref) == set(range(1, 9))


def test_marker_kinds_reject_document_support_and_bad_ids() -> None:
    fact = _fact("markets")
    row = briefs._evidence_rows_for_fact(BRIEF_ID, 1, fact)[0]
    row.fact_id = "x" * 65
    assert not ev._validate_fact_group([row], set(), set(), set())
    row.fact_id = "ok"
    row.document_id = uuid4()
    assert not ev._validate_fact_group([row], set(), set(), set())


def test_marker_citation_must_name_exactly_one_source() -> None:
    fact = _fact("hazards")
    grouped = _grouped([fact])
    base = {"ref": 1, "kind": "hazards", "id": fact["id"], "title": "t"}
    for bad in ([], [SOURCE, str(uuid4())]):
        row = SimpleNamespace(citations=[{**base, "source_ids": bad}])
        assert ev._validate_citations(row, grouped) is None  # type: ignore[arg-type]


def test_fact_id_stays_within_column_for_long_metric_and_symbol() -> None:
    row = SimpleNamespace(source_id=uuid4(), metric="m" * 500, symbol="s" * 500)
    assert len(context._obs_id(row)) <= ev.MAX_FACT_ID
    assert DailyBriefEvidence.__table__.c.fact_id.type.length == ev.MAX_FACT_ID  # type: ignore[attr-defined]


def test_marker_row_carries_its_source_id() -> None:
    row = briefs._evidence_rows_for_fact(BRIEF_ID, 1, _fact("weather"))[0]
    assert str(row.source_id) == SOURCE and row.document_id is None
    bad = {**_fact("weather"), "source_ids": [SOURCE, str(uuid4())]}
    with pytest.raises(briefs.BriefUnavailable):
        briefs._evidence_rows_for_fact(BRIEF_ID, 1, bad)


def test_marker_group_requires_source_and_captures_it() -> None:
    row = briefs._evidence_rows_for_fact(BRIEF_ID, 1, _fact("markets"))[0]
    sources: set[Any] = set()
    assert ev._validate_fact_group([row], set(), sources, set())
    assert {str(value) for value in sources} == {SOURCE}
    row.source_id = None
    assert not ev._validate_fact_group([row], set(), set(), set())


def test_tuple_check_allows_marker_source_only_for_marker_kinds() -> None:
    text = next(
        str(c.sqltext)
        for c in DailyBriefEvidence.__table__.constraints  # type: ignore[attr-defined]
        if getattr(c, "name", None) == "ck_daily_brief_evidence_document_tuple"
    )
    marker = (
        "source_id IS NOT NULL AND fact_kind IN ('markets','weather','hazards','source_health')"
    )
    assert marker in text
    assert "'tasks'" not in text


@pytest.mark.asyncio
async def test_uncited_marker_source_withholds_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """An uncited marker fact still re-gates its source: ineligible -> brief withheld."""
    cited, uncited = _fact("markets"), _fact("weather")
    other = str(uuid4())
    uncited["source_ids"] = [other]
    uncited["id"] = f"{other}:weather:"
    grouped = _grouped([cited, uncited])
    seen: dict[str, Any] = {}

    async def eligible(_s: Any, sources: set[Any], documents: set[Any], **_k: Any) -> bool:
        seen["sources"] = {str(value) for value in sources}
        return False

    async def load(*_a: Any, **_k: Any) -> Any:
        sources: set[Any] = set()
        for rows in grouped.values():
            assert ev._validate_fact_group(rows, set(), sources, set())
        return grouped, sources, set()

    monkeypatch.setattr(ev, "_load_evidence_groups", load)
    monkeypatch.setattr(ev, "_captured_support_eligible", eligible)
    row = SimpleNamespace(
        evidence_capture_version=1,
        evidence_capture_status="captured",
        evidence_fact_count=2,
        status="current",
        # Only the first fact is cited; the second still shaped the prompt.
        citations=[{"ref": 1, **{k: cited[k] for k in ("kind", "id", "title", "source_ids")}}],
    )
    ok = await ev._captured_inputs_match(
        None,  # type: ignore[arg-type]
        row,  # type: ignore[arg-type]
        scope=SimpleNamespace(),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        lock=False,
    )
    assert ok is False and seen["sources"] == {SOURCE, other}
