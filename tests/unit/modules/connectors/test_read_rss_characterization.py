"""Characterization tests for ``read_rss`` pagination, cursor filtering and truncation bookkeeping.

Drive the injected-``fetch`` path (the native pinned transport) so no network is needed; the
direct-client path is covered by ``test_f27_pinned_fetch``. They pin page walking, the overlap
floor, record/page caps, ``stats`` continuation flags and the returned cursor.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from modules.connectors import n8n

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _rss(items: list[tuple[str, datetime | None]], *, next_url: str | None = None) -> bytes:
    body = "".join(
        f"<item><guid>{guid}</guid><title>t-{guid}</title><link>/p/{guid}</link>"
        + (f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>" if when else "")
        + "<description>d</description></item>"
        for guid, when in items
    )
    link = f'<atom:link xmlns:atom="http://www.w3.org/2005/Atom" rel="next" href="{next_url}"/>' if next_url else ""
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{link}{body}</channel></rss>'.encode()


def _fetch(pages: dict[str, bytes]) -> AsyncMock:
    async def fetch(url: str) -> bytes:
        return pages[url]
    return AsyncMock(side_effect=fetch)


@pytest.mark.asyncio
async def test_single_page_records_and_cursor_after_is_newest_published_time() -> None:
    pages = {"https://f.example/rss": _rss([("a", BASE), ("b", BASE + timedelta(hours=1)), ("c", None)])}
    result = await n8n.read_rss("https://f.example/rss", None, fetch=_fetch(pages))
    records = result["records"]
    assert [r["provider_id"] for r in records] == ["a", "b", "c"]  # type: ignore[index]
    assert records[0]["metadata"]["canonical_url"] == "https://f.example/p/a"  # type: ignore[index]
    assert records[0]["metadata"]["published_at"] == BASE.isoformat()  # type: ignore[index]
    assert records[2]["metadata"]["published_at"] is None  # type: ignore[index]
    assert result["cursor_before"] is None
    assert result["cursor_after"] == (BASE + timedelta(hours=1)).isoformat()  # undated item never moves it


@pytest.mark.asyncio
async def test_overlap_floor_drops_old_items_and_keeps_cursor_when_nothing_new() -> None:
    old = BASE - timedelta(days=30)
    cursor = BASE.isoformat()
    pages = {"https://f.example/rss": _rss([("old", old)])}
    result = await n8n.read_rss("https://f.example/rss", cursor, fetch=_fetch(pages))
    assert result["records"] == [] and result["cursor_after"] == cursor


@pytest.mark.asyncio
async def test_next_links_are_followed_once_and_loops_stop() -> None:
    pages = {
        "https://f.example/rss": _rss([("a", BASE)], next_url="/rss?page=2"),
        "https://f.example/rss?page=2": _rss([("b", BASE)], next_url="https://f.example/rss"),
    }
    fetch = _fetch(pages)
    stats: dict[str, object] = {}
    result = await n8n.read_rss("https://f.example/rss", None, fetch=fetch, stats=stats)
    assert [r["provider_id"] for r in result["records"]] == ["a", "b"]  # type: ignore[index]
    assert [c.args[0] for c in fetch.await_args_list] == ["https://f.example/rss", "https://f.example/rss?page=2"]
    assert stats == {}


@pytest.mark.asyncio
async def test_record_cap_inside_a_page_marks_truncated_without_resume_url() -> None:
    items = [(f"i{n}", BASE) for n in range(501)]
    pages = {"https://f.example/rss": _rss(items, next_url="/rss?page=2")}
    stats: dict[str, object] = {}
    result = await n8n.read_rss("https://f.example/rss", None, fetch=_fetch(pages), stats=stats)
    assert len(result["records"]) == 500  # type: ignore[arg-type]
    assert stats == {"truncated": True}


@pytest.mark.asyncio
async def test_record_cap_at_page_boundary_sets_resume_url() -> None:
    items = [(f"i{n}", BASE) for n in range(500)]
    pages = {"https://f.example/rss": _rss(items, next_url="/rss?page=2")}
    stats: dict[str, object] = {}
    result = await n8n.read_rss("https://f.example/rss", None, fetch=_fetch(pages), stats=stats)
    assert len(result["records"]) == 500  # type: ignore[arg-type]
    assert stats == {"resume_url": "https://f.example/rss?page=2"}


@pytest.mark.asyncio
async def test_ten_page_cap_sets_resume_url_for_the_unread_link() -> None:
    pages = {
        f"https://f.example/rss?p={n}" if n else "https://f.example/rss": _rss([(f"g{n}", BASE)], next_url=f"/rss?p={n + 1}")
        for n in range(10)
    }
    stats: dict[str, object] = {}
    result = await n8n.read_rss("https://f.example/rss", None, fetch=_fetch(pages), stats=stats)
    assert len(result["records"]) == 10  # type: ignore[arg-type]
    assert stats == {"resume_url": "https://f.example/rss?p=10"}


@pytest.mark.asyncio
async def test_total_byte_cap_and_non_public_url_are_rejected() -> None:
    big = AsyncMock(return_value=b"x" * (25 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="RSS pagination exceeded the 25 MiB limit"):
        await n8n.read_rss("https://f.example/rss", None, fetch=big)
    with patch.object(n8n, "validate_public_url", AsyncMock(side_effect=ValueError("URL resolves to a non-public address"))), \
            pytest.raises(ValueError, match="non-public"):
        await n8n.read_rss("https://localhost/rss", None)
