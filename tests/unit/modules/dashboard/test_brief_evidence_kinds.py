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


@pytest.mark.asyncio
async def test_marker_sources_join_the_eligibility_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fact = _fact("markets")
    grouped = _grouped([fact])
    seen: dict[str, Any] = {}

    async def load(*_a: Any, **_k: Any) -> Any:
        return grouped, set(), set()

    async def eligible(_s: Any, sources: set[Any], documents: set[Any], **_k: Any) -> bool:
        seen["sources"] = {str(value) for value in sources}
        return False

    monkeypatch.setattr(ev, "_load_evidence_groups", load)
    monkeypatch.setattr(ev, "_captured_support_eligible", eligible)
    row = SimpleNamespace(
        evidence_capture_version=1,
        evidence_capture_status="captured",
        evidence_fact_count=1,
        status="current",
        citations=[{"ref": 1, **{k: fact[k] for k in ("kind", "id", "title", "source_ids")}}],
    )
    ok = await ev._captured_inputs_match(
        None,  # type: ignore[arg-type]
        row,  # type: ignore[arg-type]
        scope=SimpleNamespace(),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        lock=False,
    )
    assert ok is False and seen["sources"] == {SOURCE}
