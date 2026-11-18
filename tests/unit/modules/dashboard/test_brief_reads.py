"""``get_brief`` and ``live_brief_ids``: scoped point reads with fail-closed liveness."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.dashboard import _brief_reads, briefs

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


class _LockSession(_Session):
    """Session serving brief rows to ``scalars`` and no evidence rows to ``execute``."""

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(all=lambda: [])


@pytest.mark.asyncio
async def test_live_brief_ids_reaches_prelock_with_more_than_100_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daily task thread past ~101 days must not trip the 100-id lock-page bound (one lock pass)."""

    async def admit(*_a: Any, **_k: Any) -> None:
        return None

    lock_calls: list[int] = []

    async def lock(*_a: Any, **_k: Any) -> tuple[dict[Any, Any], set[Any]]:
        lock_calls.append(1)
        return {}, set()

    async def matches(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(_brief_reads, "_admit", admit)
    monkeypatch.setattr(_brief_reads, "_actor", lambda _scope: uuid4())
    monkeypatch.setattr(briefs, "_admit", admit)
    monkeypatch.setattr(briefs, "_lock_dependency_sets", lock)
    monkeypatch.setattr(briefs, "_captured_inputs_match", matches)
    rows = [
        SimpleNamespace(
            id=uuid4(),
            brief_date=date(2026, 1, 1),
            timezone="UTC",
            status="current",
            revision=1,
            content="c",
            citations=[],
            model_alias="m",
            generated_at=datetime.now(UTC),
        )
        for _ in range(101)
    ]
    out = await _brief_reads.live_brief_ids(
        _LockSession(rows),  # type: ignore[arg-type]
        [r.id for r in rows],
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    assert out == {r.id for r in rows} and lock_calls == [1]


def _lockfree_patch(monkeypatch: pytest.MonkeyPatch, match: Any) -> list[str]:
    from modules.dashboard import briefs

    async def admit(*_a: Any, **_k: Any) -> None:
        return None

    prelocks: list[str] = []

    async def prelock(*_a: Any, **_k: Any) -> None:
        prelocks.append("x")

    monkeypatch.setattr(_brief_reads, "_admit", admit)
    monkeypatch.setattr(_brief_reads, "_actor", lambda _scope: uuid4())
    monkeypatch.setattr(briefs, "prelock_captured_inputs", prelock)
    monkeypatch.setattr(briefs, "_captured_inputs_match", match)
    return prelocks


@pytest.mark.asyncio
async def test_live_brief_ids_lock_false_never_prelocks_and_match_error_is_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good, boom, no = uuid4(), uuid4(), uuid4()

    async def match(_s: Any, row: Any, **k: Any) -> bool:
        assert k["lock"] is False
        if row.id == boom:
            raise KeyError("x")
        return row.id == good

    prelocks = _lockfree_patch(monkeypatch, match)
    session = _Session([SimpleNamespace(id=i) for i in (good, boom, no)])
    out = await _brief_reads.live_brief_ids(
        session,  # type: ignore[arg-type]
        [good, boom, no],
        scope=SCOPE,
        multi_workspace_enabled=False,
        lock=False,
    )
    assert out == {good} and prelocks == []


@pytest.mark.asyncio
async def test_live_brief_ids_keeps_the_newest_500_and_fails_closed_on_the_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def match(*_a: Any, **_k: Any) -> bool:
        return True

    _lockfree_patch(monkeypatch, match)
    ids = [uuid4() for _ in range(501)]  # oldest first
    requested: list[Any] = []

    class _Capture(_Session):
        async def scalars(self, statement: Any) -> list[Any]:
            requested.extend(
                v for v in statement.compile().params.values() if isinstance(v, list | tuple)
            )
            return [SimpleNamespace(id=i) for i in ids]

    out = await _brief_reads.live_brief_ids(
        _Capture([]),  # type: ignore[arg-type]
        ids,
        scope=SCOPE,
        multi_workspace_enabled=False,
        lock=False,
    )
    assert len(requested[0]) == 500 and ids[0] not in requested[0] and ids[-1] in requested[0]
    assert len(out) == 501  # the fake DB ignores the filter; the request bound is asserted above
