"""Batched embedding: one request per claim, per-batch recheck/publication, identical index contents."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from modules.search import indexing

pytestmark = pytest.mark.asyncio

SOURCE_A, SOURCE_B = uuid4(), uuid4()


def _vector(content: str) -> list[float]:
    """Deterministic fake embedding so every batching of the same chunks must publish the same vectors."""
    digest = hashlib.sha256(content.encode()).digest()
    return [float(digest[0]) + 1.0, float(digest[1]) + 1.0, float(digest[2]) + 1.0]


class _World:
    """In-memory generation/items plus call recorders shared by the patched seams."""

    def __init__(self, n: int) -> None:
        self.generation = SimpleNamespace(
            id=uuid4(), status="running", dimensions=None, response_model_id=None, error_code=None
        )
        self.chunks = [
            indexing._ClaimedChunk(
                uuid4(), f"chunk text {i}", SOURCE_A if i % 2 else SOURCE_B, uuid4(), None
            )
            for i in range(n)
        ]
        self.items = {
            c.item_id: SimpleNamespace(
                id=c.item_id, status="pending", error_code=None, generation_id=None
            )
            for c in self.chunks
        }
        self.embed_inputs: list[list[str]] = []
        self.updates: dict[UUID, str] = {}
        self.deleted: list[UUID] = []
        self.stale: set[UUID] = set()
        self.recheck_order: list[UUID] = []
        self.bad_response = False

    def factory(self) -> Any:
        world = self

        class Session:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def rollback(self) -> None:
                return None

            async def scalar(self, _stmt: Any) -> Any:
                if (
                    str(_stmt).lstrip().startswith("SELECT search_index_items")
                ):  # legacy per-item lookup
                    wanted = [v for v in _stmt.compile().params.values() if v in world.items]
                    return world.items.get(wanted[0]) if wanted else None
                return world.generation

            async def scalars(self, _stmt: Any) -> list[Any]:
                return list(world.items.values())

            async def execute(self, _stmt: Any, params: Any = None) -> None:
                for row in [params] if isinstance(params, dict) else params or []:
                    world.updates[row["item_id"]] = row["embedding"]

            async def delete(self, item: Any) -> None:
                world.deleted.append(item.id)
                world.items.pop(item.id, None)

        return Session()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World(7)
    by_chunk = {c.chunk_id: c for c in w.chunks}

    async def recheck(session: Any, **kw: Any) -> tuple[Any, bool, int]:
        w.recheck_order.append(kw["source_id"])
        return "fence", by_chunk[kw["chunk_id"]].item_id not in w.stale, 1

    class Gateway:
        async def embed(
            self, _role: str, _mapping: Any, _policy: Any, inputs: list[str]
        ) -> dict[str, Any]:
            w.embed_inputs.append(list(inputs))
            if w.bad_response:
                return {"data": [], "model": "m"}
            # reversed order + explicit indexes proves rows are re-associated by index, not position
            rows = [{"index": i, "embedding": _vector(t)} for i, t in enumerate(inputs)]
            return {"data": list(reversed(rows)), "model": "m"}

    async def commit(*_a: Any, **_k: Any) -> None:
        return None

    async def projection(*_a: Any, **_k: Any) -> None:
        return None

    async def drop(_factory: Any, *, item_id: UUID, **_k: Any) -> None:
        w.deleted.append(item_id)
        w.items.pop(item_id, None)

    monkeypatch.setattr(indexing, "_recheck_prepared", recheck)
    monkeypatch.setattr(indexing, "gateway", lambda *_a, **_k: Gateway())
    monkeypatch.setattr(indexing, "_commit_index_change", commit)
    monkeypatch.setattr(indexing, "_index_projection", projection)
    monkeypatch.setattr(indexing, "_drop_pending_item", drop)
    return w


def _ctx(w: _World) -> indexing._RunContext:
    original = SimpleNamespace(mapping=object(), policy=object(), config=object(), fence="fence")
    return indexing._RunContext(
        w.factory,
        None,
        SimpleNamespace(multi_workspace_enabled=True),  # type: ignore[arg-type]
        SimpleNamespace(workspace_id=uuid4()),
        original,  # type: ignore[arg-type]
        w.generation.id,
        uuid4(),
    )


async def _run(w: _World, size: int) -> None:
    ctx, state = _ctx(w), indexing._RunState()
    for start in range(0, len(w.chunks), size):
        embedded = await indexing._embed_chunk_batch(ctx, w.chunks[start : start + size])
        assert embedded is not None
        assert await indexing._publish_embedding_batch(ctx, state, embedded)


async def test_one_request_per_batch_and_rows_follow_input_order(world: _World) -> None:
    await _run(world, 7)
    assert len(world.embed_inputs) == 1 and len(world.embed_inputs[0]) == 7
    assert world.updates == {
        c.item_id: indexing._vector_literal(_vector(c.content)) for c in world.chunks
    }
    assert all(item.status == "succeeded" for item in world.items.values())
    assert world.generation.dimensions == 3 and world.generation.response_model_id == "m"


async def test_index_contents_identical_for_every_batch_size(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = []
    for size in (1, 3, 7):
        w = _World(0)
        w.chunks = world.chunks
        w.items = {
            c.item_id: SimpleNamespace(
                id=c.item_id, status="pending", error_code=None, generation_id=None
            )
            for c in world.chunks
        }
        w.generation = SimpleNamespace(
            id=uuid4(), status="running", dimensions=None, response_model_id=None, error_code=None
        )
        # reuse the patched seams by delegating recorders to the shared fixture world
        w.embed_inputs, w.recheck_order = world.embed_inputs, world.recheck_order
        by_chunk = {c.chunk_id: c for c in w.chunks}

        async def recheck(session: Any, **kw: Any) -> tuple[Any, bool, int]:
            return "fence", True, 1

        monkeypatch.setattr(indexing, "_recheck_prepared", recheck)
        await _run(w, size)
        results.append(dict(w.updates))
        assert by_chunk
    assert results[0] == results[1] == results[2]


async def test_request_count_drops_with_batch_size(world: _World) -> None:
    await _run(world, 4)
    assert [len(x) for x in world.embed_inputs] == [4, 3]


async def test_stale_chunks_are_dropped_and_not_sent(world: _World) -> None:
    gone = world.chunks[2]
    world.stale.add(gone.item_id)
    ctx = _ctx(world)
    embedded = await indexing._embed_chunk_batch(ctx, world.chunks)
    assert embedded is not None and gone.item_id in world.deleted
    assert len(world.embed_inputs[0]) == 6 and gone.content not in world.embed_inputs[0]
    assert await indexing._publish_embedding_batch(ctx, indexing._RunState(), embedded)
    assert gone.item_id not in world.updates and len(world.updates) == 6


async def test_publication_locks_sources_in_canonical_order(world: _World) -> None:
    ctx = _ctx(world)
    embedded = await indexing._embed_chunk_batch(ctx, world.chunks)
    world.recheck_order.clear()
    assert embedded and await indexing._publish_embedding_batch(ctx, indexing._RunState(), embedded)
    assert world.recheck_order == sorted(world.recheck_order, key=str)  # Source ids ascending


async def test_malformed_response_falls_back_to_per_chunk(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.bad_response = True
    seen: list[UUID] = []

    async def single(_ctx: Any, _state: Any, claimed: Any) -> bool:
        seen.append(claimed.chunk_id)
        return True

    monkeypatch.setattr(indexing, "_process_claimed_chunk", single)
    assert (
        await indexing._process_claimed_batch(_ctx(world), indexing._RunState(), world.chunks)
        is True
    )
    assert seen == [c.chunk_id for c in world.chunks]


async def test_model_identity_change_fails_generation_and_stops(world: _World) -> None:
    world.generation.dimensions, world.generation.response_model_id = 3, "other"
    ctx = _ctx(world)
    embedded = await indexing._embed_chunk_batch(ctx, world.chunks[:2])
    assert (
        embedded
        and await indexing._publish_embedding_batch(ctx, indexing._RunState(), embedded) is False
    )
    assert (
        world.generation.status == "failed"
        and world.generation.error_code == "model_identity_changed"
    )
    assert not world.updates


async def test_batch_response_validation_and_size_clamp() -> None:
    ok = {
        "data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [1.0, 1.0]}],
        "model": "m",
    }
    vectors, model = indexing.embedding_batch_values(ok, 2, 2)
    assert vectors == [[1.0, 1.0], [0.0, 2.0]] and model == "m"
    for bad in (
        {"data": []},
        {"data": [ok["data"][0]]},
        {"data": [ok["data"][0], ok["data"][0]]},
        {"data": [{"embedding": [0.0, 0.0]}, {"embedding": [1.0, 1.0]}]},
        "x",
    ):
        with pytest.raises(ValueError):
            indexing.embedding_batch_values(bad, 2, 2)
    assert indexing.embed_batch_size(SimpleNamespace(search_embed_batch_size=500)) == 128
    assert indexing.embed_batch_size(SimpleNamespace(search_embed_batch_size=0)) == 1
    assert indexing.embed_batch_size(SimpleNamespace()) == 1  # legacy single-chunk path
    assert indexing.embed_batch_size(SimpleNamespace(search_embed_batch_size=64)) == 64


async def test_index_generation_loops_batches_until_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches = iter([["a"] * 4, ["b"] * 4, []])
    limits: list[int] = []

    async def prepare(*_a: Any) -> tuple[Any, None]:
        return object(), None

    async def claim(_ctx: Any, _state: Any, limit: int) -> list[Any]:
        limits.append(limit)
        return next(batches)

    async def process(_ctx: Any, state: Any, claimed: list[Any]) -> bool:
        state.completed += len(claimed)
        return True

    monkeypatch.setattr(indexing, "_prepare_index_run", prepare)
    monkeypatch.setattr(indexing, "_claim_chunk_batch", claim)
    monkeypatch.setattr(indexing, "_process_claimed_batch", process)
    settings = SimpleNamespace(search_embed_batch_size=32)
    assert await indexing._index_generation(None, None, settings, uuid4(), uuid4()) == 8  # type: ignore[arg-type]
    assert limits == [32, 32, 32]


async def test_batched_index_contents_equal_legacy_single_chunk_pipeline(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEARCH_EMBED_BATCH_SIZE=1 keeps the legacy per-chunk path; its output must equal the batched one."""

    async def fresh() -> _World:
        w = _World(0)
        w.chunks = world.chunks
        w.items = {
            c.item_id: SimpleNamespace(
                id=c.item_id, status="pending", error_code=None, generation_id=None
            )
            for c in world.chunks
        }
        return w

    legacy = await fresh()
    ctx, state = _ctx(legacy), indexing._RunState()
    for (
        chunk
    ) in legacy.chunks:  # the exact functions `_index_generation` loops over when batch size == 1
        assert await indexing._process_claimed_chunk(ctx, state, chunk)
    batched = await fresh()
    await _run(batched, 7)
    # the patched gateway records into the fixture world: 7 single-chunk requests, then one batched request
    assert [len(x) for x in world.embed_inputs] == [1] * 7 + [7]
    assert legacy.updates == batched.updates and legacy.updates
    assert {i: it.status for i, it in legacy.items.items()} == {
        i: it.status for i, it in batched.items.items()
    }
    assert (legacy.generation.dimensions, legacy.generation.response_model_id) == (
        batched.generation.dimensions,
        batched.generation.response_model_id,
    )


async def test_fallback_skips_chunks_dropped_as_stale(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = world.chunks[1]
    world.stale.add(gone.item_id)
    world.bad_response = True
    seen: list[UUID] = []

    async def single(_ctx: Any, _state: Any, claimed: Any) -> bool:
        seen.append(claimed.chunk_id)
        return True

    monkeypatch.setattr(indexing, "_process_claimed_chunk", single)
    assert (
        await indexing._process_claimed_batch(_ctx(world), indexing._RunState(), world.chunks)
        is True
    )
    assert gone.chunk_id not in seen and len(seen) == len(world.chunks) - 1
    assert gone.item_id in world.deleted
