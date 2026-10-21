"""Graph account erasure must stop before touching foreign partition edges."""

import asyncio
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from neo4j import AsyncGraphDatabase, Query

from core.auth.erasure_contracts import AccountErasureScope
from modules.knowledge.temporal import erasure
from modules.knowledge.temporal.graph_fence import (
    ERASE_PARTITION_FENCE,
    PARTITION_FENCE_SCHEMA,
    require_partition_fence_schema,
)


class _Session:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: object) -> UUID:
        return UUID(int=21)

    async def rollback(self) -> None:
        return None


class _Record(dict[str, Any]):
    def data(self) -> dict[str, Any]:
        return dict(self)


class _Result:
    def __init__(
        self, record: dict[str, Any] | None, *, events: list[str], name: str,
        consume_error: bool = False,
    ) -> None:
        self.record = _Record(record) if record is not None else None
        self.events, self.name, self.consume_error = events, name, consume_error

    async def single(self, *, strict: bool = False) -> _Record | None:
        return self.record

    async def consume(self) -> None:
        self.events.append(f"{self.name}:consume")
        if self.consume_error:
            raise RuntimeError("Unknown commit outcome")
        self.events.append(f"{self.name}:committed")


class _Graph:
    def __init__(
        self, *, edges: int = 0, foreign: bool = False, nodes: int = 0,
        schema_count: int = 1, fence_error: bool = False,
    ) -> None:
        self.edges, self.foreign, self.nodes = edges, foreign, nodes
        self.schema_count, self.fence_error = schema_count, fence_error
        self.erased = False
        self.queries: list[str] = []
        self.events: list[str] = []
        self.timeouts: list[float | None] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def run(self, query: Query, **_params: object) -> _Result:
        text = str(query)
        self.queries.append(text)
        self.timeouts.append(query.timeout)
        if "SET f.erased = true" in text:
            self.erased = True
            return _Result({"erased": True}, events=self.events, name="fence", consume_error=self.fence_error)
        self.events.append(text)
        row: dict[str, Any] | None = {"found": 0}
        if text == PARTITION_FENCE_SCHEMA:
            row = {"found": self.schema_count}
        elif "DELETE r" in text:
            row = {"removed": self.edges}
        elif "coalesce(r.group_id" in text:
            row = {"found": 1} if self.foreign else None
        elif "DELETE n" in text:
            row = {"removed": self.nodes}
        return _Result(row, events=self.events, name="other")


class _Driver:
    def __init__(self, graph: _Graph) -> None:
        self.graph = graph

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def session(self, **_kwargs: object) -> _Graph:
        return self.graph


def _context(monkeypatch: pytest.MonkeyPatch, graph: _Graph) -> dict[str, Any]:
    driver = Mock(return_value=_Driver(graph))
    monkeypatch.setattr(AsyncGraphDatabase, "driver", driver)
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    return {
        "session_factory": lambda: _Session(),
        "settings": SimpleNamespace(graph_host="localhost", graph_port=7687,
                                    graph_username="neo4j", graph_password=SimpleNamespace(
                                        get_secret_value=lambda: "secret"), graph_database="neo4j"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("edges,foreign", [(1, False), (0, True)])
async def test_graph_nodes_wait_for_owned_edges_and_reject_foreign_edges(
    monkeypatch: pytest.MonkeyPatch, edges: int, foreign: bool,
) -> None:
    graph = _Graph(edges=edges, foreign=foreign)
    ctx = _context(monkeypatch, graph)
    scope = AccountErasureScope(UUID(int=22), 7, UUID(int=23))
    if foreign:
        with pytest.raises(RuntimeError, match="foreign relationship"):
            await erasure.erase_account_graph_page(ctx, scope, {})
    else:
        assert await erasure.erase_account_graph_page(ctx, scope, {}) == (False, {})
    assert not any("DELETE n" in query for query in graph.queries)
    assert "CREATE CONSTRAINT" in graph.queries[0]
    assert "SET f.erased = true" in graph.queries[2]


@pytest.mark.asyncio
async def test_unknown_fence_commit_never_deletes_and_retry_preserves_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(fence_error=True)
    ctx = _context(monkeypatch, graph)
    scope = AccountErasureScope(UUID(int=22), 7, UUID(int=23))
    with pytest.raises(RuntimeError, match="Unknown commit"):
        await erasure.erase_account_graph_page(ctx, scope, {})
    assert not any("DELETE" in query for query in graph.queries)
    assert graph.erased
    graph.fence_error = False
    assert await erasure.erase_account_graph_page(ctx, scope, {}) == (False, {"after": str(UUID(int=21))})
    assert graph.erased
    assert graph.queries.count(ERASE_PARTITION_FENCE) == 2
    assert graph.events.index("fence:committed") < next(
        index for index, event in enumerate(graph.events) if "DELETE r" in event
    )


@pytest.mark.asyncio
async def test_schema_name_collision_cannot_fence_or_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _Graph(schema_count=0)
    with pytest.raises(ValueError, match="uniqueness"):
        await erasure.erase_account_graph_page(
            _context(monkeypatch, graph), AccountErasureScope(UUID(int=22), 7, UUID(int=23)), {},
        )
    assert not graph.erased
    assert not any("DELETE" in query for query in graph.queries)


@pytest.mark.parametrize("rows", [[], [{}], [{"found": True}], [{"found": 0}], [{"found": 1}, {"found": 1}]])
def test_schema_proof_must_be_exact_positive_integer(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="uniqueness"):
        require_partition_fence_schema(rows)


@pytest.mark.asyncio
async def test_empty_partition_advances_after_fence_and_absence_with_bounded_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph()
    assert await erasure.erase_account_graph_page(
        _context(monkeypatch, graph), AccountErasureScope(UUID(int=22), 7, UUID(int=23)), {},
    ) == (False, {"after": str(UUID(int=21))})
    assert "RETURN count(n) AS found" in graph.queries[-1]
    assert all(timeout == erasure.QUERY_SECONDS for timeout in graph.timeouts)
    assert graph.events.index("fence:committed") < next(
        index for index, event in enumerate(graph.events) if "DELETE" in event
    )
    assert AsyncGraphDatabase.driver.call_args.kwargs["connection_timeout"] == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stalled_graph_io_expires_without_advancing(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _Graph()
    original_timeout = asyncio.timeout
    monkeypatch.setattr(erasure.asyncio, "timeout", lambda _seconds: original_timeout(0.01))
    async def stall(*_args: object, **_kwargs: object) -> _Result:
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")
    graph.run = stall  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await erasure.erase_account_graph_page(
            _context(monkeypatch, graph), AccountErasureScope(UUID(int=22), 7, UUID(int=23)), {},
        )
    assert not graph.erased
