"""Compiled-SQL shape of the vector candidate query (must stay servable by the partial HNSW index)."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.search import public
from modules.search.schemas import SearchRequest


class _Session:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.queries: list[str] = []

    async def execute(self, stmt: Any, *_a: Any) -> None:
        self.executed.append(str(stmt))

    async def scalars(self, stmt: Any, params: Any = None) -> Any:
        self.queries.append(str(stmt))
        self.params = params
        return SimpleNamespace(all=list)


@pytest.mark.asyncio
async def test_vector_query_orders_inner_by_distance_only_and_inlines_generation() -> None:
    gen = SimpleNamespace(id=uuid4(), dimensions=3)
    session = _Session()
    await public._vector_ids(
        session, SearchRequest(query="q"), gen, [0.1, 0.2, 0.3], workspace_id=uuid4(),  # type: ignore[arg-type]
    )
    sql = session.queries[0]
    inner = sql[sql.index("FROM search_index_items"):]
    inner_order = inner[inner.index("ORDER BY"):inner.index(") t")]
    assert "chunk_id" not in inner_order
    assert "<=>" in inner_order
    assert f"i.generation_id = '{gen.id}'" in sql
    assert "i.status = 'succeeded'" in sql
    assert sql.rstrip().endswith("ORDER BY t.dist, t.chunk_id")
    assert "generation_id" not in session.params
    assert any("hnsw.iterative_scan = relaxed_order" in s for s in session.executed)
    assert any("hnsw.ef_search" in s for s in session.executed)


@pytest.mark.asyncio
async def test_vector_query_rejects_non_uuid_generation() -> None:
    gen = SimpleNamespace(id="x'; DROP TABLE a;--", dimensions=3)
    with pytest.raises(ValueError):
        await public._vector_ids(
            _Session(), SearchRequest(query="q"), gen, [0.1], workspace_id=uuid4(),  # type: ignore[arg-type]
        )
