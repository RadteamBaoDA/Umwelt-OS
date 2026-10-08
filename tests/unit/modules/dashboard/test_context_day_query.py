"""Daily context events widget must build a valid single-local-day (half-open) timeline query."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from modules.dashboard import context


@pytest.mark.asyncio
async def test_events_widget_queries_single_day_half_open(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    async def fake_list_timeline(_session, query, limit, **_scope):
        seen.append(query)
        return SimpleNamespace(items=[])

    monkeypatch.setattr(context.timeline, "list_timeline", fake_list_timeline)
    day = date(2026, 10, 7)
    widget = await context._events_widget(object(), day, "Asia/Ho_Chi_Minh", scope=object(), multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert widget.status == "empty"
    assert (seen[0].date_from, seen[0].date_to) == (day, day + timedelta(days=1))
