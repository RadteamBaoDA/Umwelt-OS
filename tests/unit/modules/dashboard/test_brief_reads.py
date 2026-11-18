"""``get_brief`` and ``live_brief_ids``: scoped point reads with fail-closed liveness."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.dashboard import _brief_reads

SCOPE: Any = SimpleNamespace(workspace_id=uuid4(), actor_id=uuid4())


class _Session:
    """Session whose ``scalars`` serves the given rows."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def scalars(self, _statement: Any) -> list[Any]:
        return self.rows


def _patch(monkeypatch: pytest.MonkeyPatch, status_by_id: dict[Any, str]) -> None:
    async def admit(*_a: Any, **_k: Any) -> None:
        return None

    async def live(_s: Any, rows: list[Any], **_k: Any) -> list[Any]:
        return [SimpleNamespace(id=r.id, status=status_by_id[r.id]) for r in rows]

    monkeypatch.setattr(_brief_reads, "_admit", admit)
    monkeypatch.setattr(_brief_reads, "_actor", lambda _scope: uuid4())
    monkeypatch.setattr(_brief_reads, "_with_live_status", live)


@pytest.mark.asyncio
async def test_live_brief_ids_drops_stale_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh, stale, missing = uuid4(), uuid4(), uuid4()
    _patch(monkeypatch, {fresh: "current", stale: "stale"})
    session = _Session([SimpleNamespace(id=fresh), SimpleNamespace(id=stale)])
    out = await _brief_reads.live_brief_ids(
        session,  # type: ignore[arg-type]
        [fresh, stale, missing],
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    assert out == {fresh}


@pytest.mark.asyncio
async def test_get_brief_none_when_absent_and_status_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid = uuid4()
    _patch(monkeypatch, {bid: "stale"})
    got = await _brief_reads.get_brief(
        _Session([SimpleNamespace(id=bid)]),  # type: ignore[arg-type]
        bid,
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    assert got is not None and got.status == "stale"
    none = await _brief_reads.get_brief(
        _Session([]),  # type: ignore[arg-type]
        bid,
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    assert none is None


@pytest.mark.asyncio
async def test_live_brief_ids_empty_input_does_no_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {})
    out = await _brief_reads.live_brief_ids(
        object(),  # type: ignore[arg-type]
        [],
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    assert out == set()
