"""Build the selected-day context from public task, goal, news and timeline providers.

Widgets always use *current* records filtered to the selected date and carry ``updated_at``;
they are never presented as historical task-state snapshots. Only the saved brief is historical.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.connectors.public import get_catalog_entry
from modules.dashboard import briefs
from modules.dashboard.daily_schemas import (
    ALL_BRIEF_SECTIONS,
    BriefSection,
    DailyContext,
    DailyWidget,
    DayRelation,
)
from modules.goals import public as goals
from modules.goals.public import GoalFilter
from modules.knowledge.observations.public import ObservationQuery, list_observations
from modules.news.public import StoryFilter, list_stories
from modules.notifications import public as notifications
from modules.sources import public as sources
from modules.tasks import public as tasks
from modules.tasks.public import TaskFilter
from modules.timeline import public as timeline
from modules.timeline.public import TimelineQuery

MAX_ITEMS = 25
WEATHER_PROVIDERS = frozenset({"open_meteo"})
_HEALTH_MAX = 5
_SOURCE_PAGES = 5  # ponytail: 500 newest sources; page further only if a workspace outgrows it.


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Return the half-open UTC window of a local calendar day (23/25h on DST days)."""
    return timeline.day_window(day, timezone)


def relation_to_today(day: date, timezone: str) -> DayRelation:
    """Classify the selected day against the current date in the selected timezone."""
    today = datetime.now(ZoneInfo(timezone)).date()
    return "past" if day < today else "future" if day > today else "today"


async def _tasks_widget(
    session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool
) -> DailyWidget:
    """Tasks due on the local day: date-only deadlines match the date, instants the local window."""
    start, end = day_bounds(day, timezone)
    by_date = await tasks.list_tasks(
        session,
        TaskFilter(due_date_from=day, due_date_to=day, limit=100),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    by_instant = await tasks.list_tasks(
        session,
        TaskFilter(due_at_from=start, due_at_to=end - timedelta(microseconds=1), limit=100),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    merged = {item.id: item for item in (*by_date.items, *by_instant.items)}
    rows = sorted(
        merged.values(), key=lambda item: (item.status in ("done", "cancelled"), item.title)
    )[:MAX_ITEMS]
    return DailyWidget(
        id="tasks",
        module="tasks",
        title_key="dayTasks",
        status="ok" if rows else "empty",
        updated_at=max((item.updated_at for item in rows), default=None),
        items=[
            {
                "id": str(item.id),
                "title": item.title,
                "status": item.status,
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "goal_id": str(item.goal_id) if item.goal_id else None,
            }
            for item in rows
        ],
    )


async def _goals_widget(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> DailyWidget:
    """Active goals with progress; goals are not date-scoped so the widget says so via source_status."""
    page = await goals.list_goals(
        session,
        GoalFilter(status="active", limit=MAX_ITEMS),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return DailyWidget(
        id="goals",
        module="goals",
        title_key="dayGoals",
        status="ok" if page.items else "empty",
        updated_at=max((item.updated_at for item in page.items), default=None),
        source_status="active_goals",
        items=[
            {
                "id": str(item.id),
                "title": item.title,
                "progress": item.progress,
                "deadline": item.deadline.isoformat() if item.deadline else None,
            }
            for item in page.items
        ],
    )


async def _stories_widget(
    session: AsyncSession,
    day: date,
    timezone: str,
    relation: DayRelation,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DailyWidget:
    """Stories observed on the local day; future days never fabricate news."""
    if relation == "future":
        return DailyWidget(
            id="stories", module="news", title_key="dayStories", status="not_applicable"
        )
    start, end = day_bounds(day, timezone)
    page = await list_stories(
        session,
        StoryFilter(date_from=start, date_to=end, limit=10),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return DailyWidget(
        id="stories",
        module="news",
        title_key="dayStories",
        status="ok" if page.items else "empty",
        updated_at=page.as_of,
        source_status=None if page.capability == "available" else page.capability,
        items=[
            {
                "id": str(item.id),
                "title": item.title,
                "source_count": item.source_count,
                "why_relevant": item.why_relevant,
                "observed_at": item.observed_at.isoformat(),
                "source_ids": sorted({str(ev.source_id) for ev in item.evidence}),
            }
            for item in page.items
        ],
    )


async def _events_widget(
    session: AsyncSession, day: date, timezone: str, *, scope: Scope, multi_workspace_enabled: bool
) -> DailyWidget:
    """Timeline events on the day; with no calendar connector only manual/imported events appear."""
    page = await timeline.list_timeline(
        session,
        TimelineQuery(date_from=day, date_to=day + timedelta(days=1), timezone=timezone),
        limit=MAX_ITEMS,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return DailyWidget(
        id="events",
        module="timeline",
        title_key="dayEvents",
        status="ok" if page.items else "empty",
        updated_at=max((item.updated_at for item in page.items), default=None),
        source_status="manual_or_imported_only",
        items=[
            {
                "id": str(item.id),
                "title": item.title,
                "type": item.type,
                "origin": item.origin,
                "source_id": str(item.source_id) if item.source_id else None,
                "source_ids": sorted({str(evidence["source_id"]) for evidence in item.evidence}),
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "occurred_date": item.occurred_date.isoformat() if item.occurred_date else None,
            }
            for item in page.items
        ],
    )


_FACT_TITLE_KEYS = {
    "markets": "dayMarkets",
    "weather": "dayWeather",
    "hazards": "dayHazards",
    "source_health": "daySourceHealth",
}


def _fact_widget(widget_id: str, items: list[dict[str, str]], as_of: datetime) -> DailyWidget:
    """Wrap fact items (``id``/``title``/``detail``/``source_id``) into a widget.

    No items gives ``status="empty"`` so the brief fact filter produces nothing for the section.
    At most ``MAX_ITEMS`` items are kept.
    """
    return DailyWidget(
        id=widget_id,
        module="dashboard",
        title_key=_FACT_TITLE_KEYS[widget_id],
        status="ok" if items else "empty",
        updated_at=as_of if items else None,
        items=items[:MAX_ITEMS],
    )


def _fmt(value: float) -> str:
    """Format a number compactly with thousands separators (``103.0`` -> ``103``)."""
    return f"{value:,.10g}"


async def _active_sources(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> list[Any]:
    """Read up to ``_SOURCE_PAGES`` pages of owner sources, keeping active, non-local-only ones."""
    rows: list[Any] = []
    cursor: str | None = None
    for _ in range(_SOURCE_PAGES):
        page, cursor = await sources.list_sources(
            session, 100, cursor, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        rows.extend(page)
        if cursor is None:
            break
    return [row for row in rows if row.status == "active" and not row.local_only]


def _kind_of(provider: str | None) -> str | None:
    """Classify a provider as ``markets``, ``weather`` or ``hazards`` from the connector catalog."""
    if not provider:
        return None
    if provider in WEATHER_PROVIDERS:
        return "weather"
    category = getattr(get_catalog_entry(provider), "category", None)
    return {"economy": "markets", "hazards": "hazards"}.get(category or "")


async def _points(
    session: AsyncSession,
    source_ids: list[Any],
    start: datetime,
    end: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Read non-null observations in ``[start, end]`` for sources (32 ids per query), oldest first.

    ponytail: one 100-row page per chunk; a busy chunk is truncated, add cursor paging if needed.
    """
    rows: list[Any] = []
    for i in range(0, len(source_ids), 32):
        page, *_ = await list_observations(
            session,
            ObservationQuery(
                source_ids=source_ids[i : i + 32], from_at=start, to_at=end, limit=100
            ),
            None,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        rows.extend(page)
    return sorted((r for r in rows if r.value is not None), key=lambda r: r.observed_at)


def _obs_id(row: Any) -> str:
    """Stable fact id ``source:metric:symbol`` of one observation series."""
    return f"{row.source_id}:{row.metric}:{row.symbol or ''}"


def _markets_items(points: list[Any]) -> list[dict[str, str]]:
    """Per series: latest value and the change against the earliest point (omitted for one point)."""
    series: dict[str, list[Any]] = defaultdict(list)
    for row in points:
        series[_obs_id(row)].append(row)
    items = []
    for key, rows in series.items():
        first, last = rows[0], rows[-1]
        pct = ""
        if len(rows) > 1 and first.value:
            pct = f" ({(last.value - first.value) / abs(first.value) * 100:+.1f}% 24h)"
        items.append(
            {
                "id": key,
                "title": last.symbol or last.metric,
                "detail": f"{_fmt(last.value)} {last.unit}{pct}",
                "source_id": str(last.source_id),
            }
        )
    return items


def _weather_items(points: list[Any]) -> list[dict[str, str]]:
    """Per source: temperature min/max and summed precipitation; sources without temperature skip."""
    by_source: dict[Any, list[Any]] = defaultdict(list)
    for row in points:
        by_source[row.source_id].append(row)
    items = []
    for source_id, rows in by_source.items():
        temps = [r for r in rows if r.metric.startswith("temperature")]
        if not temps:
            continue
        rain = sum(r.value for r in rows if r.metric.startswith("precipitation"))
        low, high = min(r.value for r in temps), max(r.value for r in temps)
        items.append(
            {
                "id": f"{source_id}:weather:",
                "title": rows[0].region or "weather",
                "detail": f"{low:.1f}–{high:.1f}{temps[0].unit}, rain {rain:.1f} mm",
                "source_id": str(source_id),
            }
        )
    return items


def _hazard_items(points: list[Any]) -> list[dict[str, str]]:
    """Top five readings by value, ties broken by the most recent ``observed_at``."""
    ranked = sorted(points, key=lambda r: (r.value, r.observed_at), reverse=True)
    return [
        {
            "id": _obs_id(r),
            "title": r.region or r.symbol or r.metric,
            "detail": f"{r.metric} {r.value} {r.region or ''}",
            "source_id": str(r.source_id),
        }
        for r in ranked[:5]
    ]


async def _observation_widgets(
    session: AsyncSession,
    rows: list[Any],
    wanted: set[str],
    as_of: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[str, DailyWidget]:
    """Build the wanted markets/weather/hazards widgets with every window anchored on ``as_of``.

    Markets and hazards read ``[as_of-24h, as_of]``; weather reads the forecast ``[as_of, as_of+24h]``
    and falls back to the last 3 h when that is empty. Reads only the given (already vetted) sources.
    """
    kwargs: Any = {"scope": scope, "multi_workspace_enabled": multi_workspace_enabled}
    ids = {k: [r.id for r in rows if _kind_of(r.provider) == k] for k in wanted}
    day = timedelta(hours=24)
    out: dict[str, list[dict[str, str]]] = {k: [] for k in wanted}
    if ids.get("markets"):
        pts = await _points(session, ids["markets"], as_of - day, as_of, **kwargs)
        out["markets"] = _markets_items(pts)
    if ids.get("hazards"):
        pts = await _points(session, ids["hazards"], as_of - day, as_of, **kwargs)
        out["hazards"] = _hazard_items(pts)
    if ids.get("weather"):
        pts = await _points(session, ids["weather"], as_of, as_of + day, **kwargs)
        pts = pts or await _points(
            session, ids["weather"], as_of - timedelta(hours=3), as_of, **kwargs
        )
        out["weather"] = _weather_items(pts)
    return {k: _fact_widget(k, items, as_of) for k, items in out.items()}


def _health_widget(rows: list[Any], as_of: datetime) -> DailyWidget:
    """List active sources whose latest error is newer than their latest success (max five)."""
    items = [
        {
            "id": str(r.id),
            "title": r.name,
            "detail": r.last_error_code or r.collection_error_code or "error",
            "source_id": str(r.id),
        }
        for r in rows
        if r.last_error_at is not None
        and (r.last_success_at is None or r.last_error_at > r.last_success_at)
    ]
    return _fact_widget("source_health", items[:_HEALTH_MAX], as_of)


async def build_daily_widgets(
    session: AsyncSession,
    day: date,
    timezone: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    relation: DayRelation | None = None,
    as_of: datetime | None = None,
    sections: Sequence[BriefSection] | None = None,
) -> list[DailyWidget]:
    """Project current owner records for one day without reading saved brief history or notifications.

    ``sections`` (default: all, in canonical order) selects widgets and emits them in the given order: stories, markets, weather, hazards,
    agenda (tasks, goals, events), source_health. ``as_of`` (default: now truncated to the minute)
    anchors every observation window so repeated calls with one value are reproducible. Widget
    queries keep their per-owner bounds and privacy filters; fact items carry ``source_id`` so the
    brief fact filter drops paused or local-only sources. Requires owner admission; read-only and
    never a historical state snapshot.
    """
    await briefs._admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    selected_relation = relation or relation_to_today(day, timezone)
    anchor = as_of or datetime.now(UTC).replace(second=0, microsecond=0)
    ordered = tuple(dict.fromkeys(sections if sections is not None else ALL_BRIEF_SECTIONS))
    wanted: set[str] = set(ordered)
    kwargs: Any = {"scope": scope, "multi_workspace_enabled": multi_workspace_enabled}
    obs_kinds = wanted & {"markets", "weather", "hazards"}
    rows: list[Any] = []
    if obs_kinds or "source_health" in wanted:
        rows = await _active_sources(session, **kwargs)
    observed = (
        await _observation_widgets(session, rows, obs_kinds, anchor, **kwargs) if obs_kinds else {}
    )
    out: list[DailyWidget] = []
    for section in ordered:
        if section == "stories":
            out.append(await _stories_widget(session, day, timezone, selected_relation, **kwargs))
        elif section == "agenda":
            out.append(await _tasks_widget(session, day, timezone, **kwargs))
            out.append(await _goals_widget(session, **kwargs))
            out.append(await _events_widget(session, day, timezone, **kwargs))
        elif section == "source_health":
            out.append(_health_widget(rows, anchor))
        else:
            out.append(observed[section])
    return out


async def build_daily_context(
    session: AsyncSession,
    day: date,
    timezone: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DailyContext:
    """Compose the saved latest brief and current-record widgets for one local day.

    Retained brief history is presentation data; callers needing only fact support should use
    ``build_daily_widgets`` so malformed saved citations cannot prevent independent current reads.
    Only the legacy ``agenda`` and ``stories`` widgets are returned (tasks, goals, events, stories).
    """
    await briefs._admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    relation = relation_to_today(day, timezone)
    widgets = await build_daily_widgets(
        session,
        day,
        timezone,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        relation=relation,
        # Brief-only fact widgets (markets, weather, hazards, source health) stay off the day view.
        sections=("agenda", "stories"),
    )
    now = datetime.now(UTC)
    return DailyContext(
        selected_date=day,
        timezone=timezone,
        relation=relation,
        generated_at=now,
        brief=await briefs.latest_brief(
            session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ),
        brief_revisions=await briefs.revision_count(
            session, day, timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ),
        widgets_updated_at=now,
        unread_notifications=(
            await notifications.list_notifications(
                session,
                unread_only=True,
                limit=1,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ).unread_count,
        widgets=widgets,
    )
