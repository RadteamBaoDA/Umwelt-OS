"""Bounded batch indexing: keyset claims, coalesced progress events, run deadline, G3 purge."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.search import indexing
from tests.unit.modules.search.test_indexing_fences import (
    SETTINGS,
    WORKSPACE,
    _Factory,
    _generation,
    _patch_generation_run,
)

pytestmark = pytest.mark.asyncio


def _setup(monkeypatch: pytest.MonkeyPatch, n: int, status: str = "active") -> tuple[_Factory, Any, list[Any]]:
    generation = _generation()
    generation.status = status
    item = SimpleNamespace(id=uuid4(), generation_id=uuid4(), status="pending", error_code=None)
    scripts: list[list[Any]] = [[generation]]
    for _ in range(n):
        scripts += [[generation, None], [], [generation, item]]
    scripts.append([generation])  # final keyset probe
    factory = _Factory(scripts, [])
    _patch_generation_run(monkeypatch, factory, stale=False)
    for _ in range(n - 1):
        factory.rows.append((uuid4(), "text", uuid4()))
    publishes: list[bool] = []

    async def commit(*_a: Any, publish: bool = True, **_k: Any) -> None:
        publishes.append(publish)

    monkeypatch.setattr(indexing, "_commit_index_change", commit)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not finalize on a keyset probe")

    monkeypatch.setattr(indexing, "activate_generation", boom)
    return factory, generation, publishes


async def test_claims_use_keyset_and_coalesce_events(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, generation, publishes = _setup(monkeypatch, 3)
    done = await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert done == 3
    sql = [q for q in (str(s.compile()) for s in factory.stmts) if "LIMIT" in q]
    assert "document_chunks.id >" not in sql[0]
    assert all("document_chunks.id >" in q for q in sql[1:]), [q[-200:] for q in sql]
    # active generation: claims never publish; only the first success in the 5s window does.
    assert publishes == [False, True, False, False, False, False]
    assert generation.status == "active"


async def test_queued_first_claim_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _g, publishes = _setup(monkeypatch, 1, status="queued")
    await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert publishes[0] is True


async def test_keyset_probe_never_finalizes_running_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, generation, publishes = _setup(monkeypatch, 1, status="running")
    done = await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert done == 1 and generation.status == "running" and len(publishes) == 2


async def test_deadline_stops_new_items(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _g, _p = _setup(monkeypatch, 3)
    clock = {"t": 0.0}
    monkeypatch.setattr(indexing, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    orig = indexing._recheck_prepared

    async def recheck(*a: Any, **k: Any) -> Any:
        clock["t"] = 1000.0  # first item finishes after the run deadline
        return await orig(*a, **k)

    monkeypatch.setattr(indexing, "_recheck_prepared", recheck)
    done = await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert done == 1


async def test_purge_version_embeddings_scoped_to_one_version() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    class S:
        async def execute(self, stmt: Any, params: Any) -> None:
            seen.append((str(stmt), params))

    version = uuid4()
    await indexing.purge_version_embeddings(S(), version)  # type: ignore[arg-type]
    sql, params = seen[0]
    assert sql.startswith("DELETE FROM search_index_items WHERE chunk_id IN")
    assert "document_version_id = :v" in sql and params == {"v": version}
