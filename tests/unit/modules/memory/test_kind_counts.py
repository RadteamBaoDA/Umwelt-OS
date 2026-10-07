"""Unit tests for per-kind Memory counts on the list page (BM-16): counts mirror the verified list."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from core.pagination import encode_cursor
from modules.memory import public
from modules.memory.public import MemoryService
from modules.memory.schemas import MemoryPage


def _rows(kinds: list[str]) -> list[SimpleNamespace]:
    base = datetime.now(UTC)
    return [
        SimpleNamespace(id=uuid4(), memory_type=k, created_at=base - timedelta(seconds=i))
        for i, k in enumerate(kinds)
    ]


async def _page(rows: list[SimpleNamespace], hidden: set[int] | None = None, **kwargs: object) -> MemoryPage:
    """Run get_memories with `rows` served in batches; rows whose index is in `hidden` fail verification."""
    hidden_ids = {rows[i].id for i in (hidden or set())}
    pending = list(rows)

    async def scalars(_stmt: object) -> MagicMock:
        batch, pending[:] = pending[:public._COUNT_BATCH], pending[public._COUNT_BATCH:]
        return MagicMock(all=lambda: batch)

    async def verified(_session: object, row: SimpleNamespace) -> object | None:
        return None if row.id in hidden_ids else SimpleNamespace()

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=scalars)
    with (
        patch.object(public, "lock_export_privacy", AsyncMock()),
        patch.object(public, "_verified_memory_read", verified),
    ):
        return await MemoryService(session).get_memories(**kwargs)  # type: ignore[arg-type]


async def test_hidden_rows_are_not_counted() -> None:
    rows = _rows(["fact", "fact", "preference", "fact", "preference"])
    page = await _page(rows, hidden={0, 2})  # 2 of 5 fail verification
    assert page.kind_counts == {"fact": 2, "preference": 1}
    assert page.total_count == 3
    assert page.counts_capped is False


async def test_counts_never_exceed_visible_list() -> None:
    rows = _rows(["fact"] * 4 + ["preference"] * 2)
    page = await _page(rows, hidden={1, 4})
    assert page.total_count == 4 and page.kind_counts == {"fact": 3, "preference": 1}


async def test_type_filter_skips_counts() -> None:
    page = await _page(_rows(["fact", "preference", "fact"]), memory_type="preference", hidden={0, 1, 2})
    assert page.kind_counts is None and page.total_count is None and page.counts_capped is False


async def test_scan_cap_looks_identical_to_verified_cap() -> None:
    """Many hidden rows and 1 visible: capped with no numbers, same shape as the >200 visible case."""
    n = public.COUNT_SCAN_CAP + public._COUNT_BATCH
    page = await _page(_rows(["fact"] * n), hidden=set(range(1, n)))
    over = await _page(_rows(["fact"] * (public.COUNT_VERIFIED_CAP + 1)))
    assert (page.counts_capped, page.kind_counts, page.total_count) == (True, None, None)
    assert (page.counts_capped, page.kind_counts, page.total_count) == (
        over.counts_capped, over.kind_counts, over.total_count,
    )


async def test_capped_returns_flag_and_no_numbers() -> None:
    page = await _page(_rows(["fact"] * (public.COUNT_VERIFIED_CAP + 1)))
    assert page.counts_capped is True
    assert page.kind_counts is None and page.total_count is None


async def test_exactly_at_cap_is_not_capped() -> None:
    page = await _page(_rows(["fact"] * public.COUNT_VERIFIED_CAP))
    assert page.counts_capped is False and page.total_count == public.COUNT_VERIFIED_CAP


async def test_cursor_pages_omit_counts() -> None:
    page = await _page([], cursor=encode_cursor(datetime.now(UTC), uuid4()))
    assert page.total_count is None and page.kind_counts is None and page.counts_capped is False


def test_counts_share_the_list_visibility_filters() -> None:
    """Deleted/forgotten rows are excluded by status; the search filter is shared with the list."""
    sql = " ".join(
        str(c.compile(dialect=postgresql.dialect())) for c in public._list_filters("active", " tea ")
    )
    assert "memories.status =" in sql and "ILIKE" in sql.upper()
    assert len(public._list_filters("forgotten", None)) == 1
