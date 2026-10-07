"""Event title/summary search reuses the listing visibility filters (P15 T2, BM-22)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from modules.timeline.public import _list_partition, list_events
from modules.timeline.schemas import TimelineQuery

BS = chr(92)


async def _compiled(query: TimelineQuery, partition: int = 0) -> tuple[str, dict[str, object]]:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    await _list_partition(session, query, partition, None, 10)
    compiled = session.scalars.await_args.args[0].compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


@pytest.mark.parametrize("partition", [0, 1, 2])
@pytest.mark.asyncio
async def test_search_keeps_deleted_and_evidence_fences(partition: int) -> None:
    """Tombstoned events, and derived events whose evidence a purge removed, stay hidden under q."""
    plain, _ = await _compiled(TimelineQuery(), partition)
    searched, params = await _compiled(TimelineQuery(q="trip"), partition)
    for fence in ("timeline_events.deleted_at IS NULL", "timeline_event_evidence"):
        assert fence in plain and fence in searched
    assert "timeline_events.title ILIKE" in searched and "timeline_events.summary ILIKE" in searched
    assert "%trip%" in params.values()
    assert "LIMIT" in searched


@pytest.mark.asyncio
async def test_like_metacharacters_are_literal() -> None:
    _, params = await _compiled(TimelineQuery(q=f"50%_off{BS}"))
    assert f"%50{BS}%{BS}_off{BS}{BS}%" in params.values()


def test_q_length_and_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        TimelineQuery(q="x" * 201)
    with pytest.raises(ValidationError):
        TimelineQuery(q="   ")


@pytest.mark.asyncio
async def test_blank_q_via_list_events_is_value_error() -> None:
    with pytest.raises(ValueError):
        await list_events(AsyncMock(), q="  ", source_id=uuid4())


@pytest.mark.asyncio
async def test_cursor_is_bound_to_q() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    cursor_page = await list_events(session, q="a", limit=1)
    assert cursor_page.next_cursor is None
