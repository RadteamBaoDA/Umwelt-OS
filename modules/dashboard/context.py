"""Build the selected-day context from public task, goal, news and timeline providers.

Widgets always use *current* records filtered to the selected date and carry ``updated_at``;
they are never presented as historical task-state snapshots. Only the saved brief is historical.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.dashboard import briefs
from modules.dashboard.daily_schemas import DailyContext, DailyWidget, DayRelation
from modules.goals import public as goals
from modules.goals.schemas import GoalFilter
from modules.news.public import StoryFilter, list_stories
from modules.notifications import public as notifications
from modules.tasks import public as tasks
from modules.tasks.schemas import TaskFilter
from modules.timeline import public as timeline
from modules.timeline.schemas import TimelineQuery

MAX_ITEMS = 25


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Return the half-open UTC window of a local calendar day (23/25h on DST days)."""
    return timeline.day_window(day, timezone)


def relation_to_today(day: date, timezone: str) -> DayRelation:
    """Classify the selected day against the current date in the selected timezone."""
    today = datetime.now(ZoneInfo(timezone)).date()
    return "past" if day < today else "future" if day > today else "today"


async def _tasks_widget(session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool) -> DailyWidget:
    """Tasks due on the local day: date-only deadlines match the date, instants the local window."""
    start, end = day_bounds(day, timezone)
    by_date = await tasks.list_tasks(session, TaskFilter(due_date_from=day, due_date_to=day, limit=100), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    by_instant = await tasks.list_tasks(
        session, TaskFilter(due_at_from=start, due_at_to=end - timedelta(microseconds=1), limit=100), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    merged = {item.id: item for item in (*by_date.items, *by_instant.items)}
    rows = sorted(merged.values(), key=lambda item: (item.status in ("done", "cancelled"), item.title))[:MAX_ITEMS]
    return DailyWidget(
        id="tasks", module="tasks", title_key="dayTasks", status="ok" if rows else "empty",
        updated_at=max((item.updated_at for item in rows), default=None),
        items=[{
            "id": str(item.id), "title": item.title, "status": item.status,
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "goal_id": str(item.goal_id) if item.goal_id else None,
        } for item in rows],
    )


async def _goals_widget(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> DailyWidget:
    """Active goals with progress; goals are not date-scoped so the widget says so via source_status."""
    page = await goals.list_goals(session, GoalFilter(status="active", limit=MAX_ITEMS), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return DailyWidget(
        id="goals", module="goals", title_key="dayGoals", status="ok" if page.items else "empty",
        updated_at=max((item.updated_at for item in page.items), default=None), source_status="active_goals",
        items=[{
            "id": str(item.id), "title": item.title, "progress": item.progress,
            "deadline": item.deadline.isoformat() if item.deadline else None,
        } for item in page.items],
    )


async def _stories_widget(
    session: AsyncSession, day: date, timezone: str, relation: DayRelation, *, scope: Scope, multi_workspace_enabled: bool,
) -> DailyWidget:
    """Stories observed on the local day; future days never fabricate news."""
    if relation == "future":
        return DailyWidget(id="stories", module="news", title_key="dayStories", status="not_applicable")
    start, end = day_bounds(day, timezone)
    page = await list_stories(
        session, StoryFilter(date_from=start, date_to=end, limit=10), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return DailyWidget(
        id="stories", module="news", title_key="dayStories", status="ok" if page.items else "empty",
        updated_at=page.as_of, source_status=None if page.capability == "available" else page.capability,
        items=[{
            "id": str(item.id), "title": item.title, "source_count": item.source_count,
            "why_relevant": item.why_relevant, "observed_at": item.observed_at.isoformat(),
            "source_ids": sorted({str(ev.source_id) for ev in item.evidence}),
        } for item in page.items],
    )


async def _events_widget(session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool) -> DailyWidget:
    """Timeline events on the day; with no calendar connector only manual/imported events appear."""
    page = await timeline.list_timeline(
        session, TimelineQuery(date_from=day, date_to=day + timedelta(days=1), timezone=timezone), limit=MAX_ITEMS, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return DailyWidget(
        id="events", module="timeline", title_key="dayEvents", status="ok" if page.items else "empty",
        updated_at=max((item.updated_at for item in page.items), default=None),
        source_status="manual_or_imported_only",
        items=[{
            "id": str(item.id), "title": item.title, "type": item.type, "origin": item.origin,
            "source_id": str(item.source_id) if item.source_id else None,
            "source_ids": sorted({str(evidence["source_id"]) for evidence in item.evidence}),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "occurred_date": item.occurred_date.isoformat() if item.occurred_date else None,
        } for item in page.items],
    )


async def build_daily_widgets(
    session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool, relation: DayRelation | None = None,
) -> list[DailyWidget]:
    """Project current owner records for one day without reading saved brief history or notifications.

    Widget queries retain their existing per-owner bounds and evidence/privacy filters. The result
    contains current task/goal/news/timeline projections, never a historical state snapshot.
    """
    await briefs._admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    selected_relation = relation or relation_to_today(day, timezone)
    return [
        await _tasks_widget(session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        await _goals_widget(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        await _stories_widget(session, day, timezone, selected_relation, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        await _events_widget(session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
    ]


async def build_daily_context(
    session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> DailyContext:
    """Compose the saved latest brief and current-record widgets for one local day.

    Retained brief history is presentation data; callers needing only fact support should use
    ``build_daily_widgets`` so malformed saved citations cannot prevent independent current reads.
    """
    await briefs._admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    relation = relation_to_today(day, timezone)
    widgets = await build_daily_widgets(session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled, relation=relation)
    now = datetime.now(UTC)
    return DailyContext(
        selected_date=day, timezone=timezone, relation=relation, generated_at=now,
        brief=await briefs.latest_brief(session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        brief_revisions=await briefs.revision_count(session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        widgets_updated_at=now,
        unread_notifications=(await notifications.list_notifications(session, unread_only=True, limit=1, scope=scope, multi_workspace_enabled=multi_workspace_enabled)).unread_count,
        widgets=widgets,
    )
