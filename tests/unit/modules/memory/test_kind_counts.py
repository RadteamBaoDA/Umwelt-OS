"""Unit tests for per-kind Memory counts on the list page (BM-16)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from core.pagination import encode_cursor
from modules.memory import public
from modules.memory.public import MemoryService
from modules.memory.schemas import MemoryPage


def _service(counts: list[tuple[str, int]]) -> tuple[MemoryService, AsyncMock]:
    session = MagicMock()
    result = MagicMock()
    result.tuples.return_value.all.return_value = counts
    session.execute = AsyncMock(return_value=result)
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    return MemoryService(session), session.execute


async def _page(svc: MemoryService, **kwargs: object) -> MemoryPage:
    with patch.object(public, "lock_export_privacy", AsyncMock()):
        return await svc.get_memories(**kwargs)  # type: ignore[arg-type]


async def test_mixed_kinds_total_is_sum_and_single_aggregate_query() -> None:
    svc, execute = _service([("fact", 3), ("preference", 2)])
    page = await _page(svc)
    assert page.kind_counts == {"fact": 3, "preference": 2}
    assert page.total_count == 5
    execute.assert_awaited_once()


async def test_type_filter_total_uses_that_kind_and_keeps_all_kinds() -> None:
    svc, _ = _service([("fact", 3), ("preference", 2)])
    page = await _page(svc, memory_type="preference")
    assert page.total_count == 2 and page.kind_counts == {"fact": 3, "preference": 2}
    page = await _page(svc, memory_type="instruction")
    assert page.total_count == 0


async def test_cursor_pages_omit_counts() -> None:
    svc, execute = _service([("fact", 1)])
    page = await _page(svc, cursor=encode_cursor(datetime.now(UTC), uuid4()))
    assert page.total_count is None and page.kind_counts is None
    execute.assert_not_awaited()


def test_counts_share_the_list_visibility_filters() -> None:
    """Deleted/forgotten rows are excluded by status; the search filter is shared with the list."""
    sql = " ".join(
        str(c.compile(dialect=postgresql.dialect())) for c in public._list_filters("active", " tea ")
    )
    assert "memories.status =" in sql and "ILIKE" in sql.upper()
    assert len(public._list_filters("forgotten", None)) == 1
