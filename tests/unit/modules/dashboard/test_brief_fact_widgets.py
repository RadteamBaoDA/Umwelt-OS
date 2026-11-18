"""Market, weather, hazard and source-health brief fact widgets, pinned to one ``as_of``."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.dashboard import briefs, context
from modules.dashboard.daily_schemas import DailyWidget

AS_OF = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
SCOPE = SimpleNamespace(workspace_id=uuid4(), actor_user_id=1, user_id=1)
CATEGORY = {"econ": "economy", "meteo": "open_meteo", "haz": "hazards"}


def _source(provider: str, **kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(),
        "name": provider,
        "provider": provider,
        "status": "active",
        "local_only": False,
        "last_error_at": None,
        "last_success_at": None,
        "last_error_code": None,
        "collection_error_code": None,
    }
    return SimpleNamespace(**{**base, **kw})


def _obs(src: SimpleNamespace, metric: str, value: float, at: datetime, **kw: Any) -> Any:
    return SimpleNamespace(
        source_id=src.id,
        provider=src.provider,
        metric=metric,
        symbol=kw.get("symbol"),
        region=kw.get("region"),
        observed_at=at,
        value=value,
        unit=kw.get("unit", "USD"),
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake admission, sources, catalog and observations (filtered by the query window)."""
    w = SimpleNamespace(sources=[], obs=[])

    async def _admit(*_a: Any, **_k: Any) -> None:
        return None

    async def list_sources(*_a: Any, **_k: Any) -> tuple[list[Any], None]:
        return list(w.sources), None

    async def list_observations(_s: Any, query: Any, *_a: Any, **_k: Any) -> Any:
        ids = set(query.source_ids)
        rows = tuple(
            o for o in w.obs if o.source_id in ids and query.from_at <= o.observed_at <= query.to_at
        )
        return rows, None, False, {}

    def entry(provider: str) -> Any:
        key = {"econ": "economy", "haz": "hazards"}.get(provider)
        return SimpleNamespace(category=key)

    async def empty(*_a: Any, **_k: Any) -> DailyWidget:
        return DailyWidget(id="x", module="x", title_key="x", status="empty")

    monkeypatch.setattr(briefs, "_admit", _admit)
    monkeypatch.setattr(context.sources, "list_sources", list_sources)
    monkeypatch.setattr(context, "list_observations", list_observations)
    monkeypatch.setattr(context, "get_catalog_entry", entry)
    for name, wid in (
        ("_tasks_widget", "tasks"),
        ("_goals_widget", "goals"),
        ("_stories_widget", "stories"),
        ("_events_widget", "events"),
    ):

        async def fake(*_a: Any, _wid: str = wid, **_k: Any) -> DailyWidget:
            return DailyWidget(id=_wid, module=_wid, title_key=_wid, status="empty")

        monkeypatch.setattr(context, name, fake)
    return w


async def _build(sections: Sequence[Any] | None = None, as_of: datetime = AS_OF) -> list[Any]:
    return await context.build_daily_widgets(
        None,  # type: ignore[arg-type]
        date(2026, 11, 18),
        "UTC",
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        as_of=as_of,
        sections=sections,
    )


async def test_sections_filter_and_order(world: SimpleNamespace) -> None:
    widgets = await _build(["markets", "stories"])
    assert [w.id for w in widgets] == ["markets", "stories"]


async def test_default_sections_include_agenda_widgets(world: SimpleNamespace) -> None:
    ids = {w.id for w in await _build(None)}
    assert ids >= {
        "tasks",
        "goals",
        "events",
        "stories",
        "markets",
        "weather",
        "hazards",
        "source_health",
    }


async def test_day_context_returns_only_legacy_widgets(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def latest(*_a: Any, **_k: Any) -> None:
        return None

    async def count(*_a: Any, **_k: Any) -> int:
        return 0

    world.sources = [_source("econ")]
    monkeypatch.setattr(briefs, "latest_brief", latest)
    monkeypatch.setattr(briefs, "revision_count", count)

    async def unread(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(unread_count=0)

    monkeypatch.setattr(context.notifications, "list_notifications", unread)
    ctx = await context.build_daily_context(
        None,  # type: ignore[arg-type]
        date(2026, 11, 18),
        "UTC",
        scope=SCOPE,  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )
    assert {w.id for w in ctx.widgets} == {"tasks", "goals", "events", "stories"}


async def test_fact_widget_title_keys_are_camel_case(world: SimpleNamespace) -> None:
    widgets = await _build(["markets", "weather", "hazards", "source_health"])
    assert [w.title_key for w in widgets] == [
        "dayMarkets",
        "dayWeather",
        "dayHazards",
        "daySourceHealth",
    ]


async def test_local_only_source_excluded(world: SimpleNamespace) -> None:
    src = _source("econ", local_only=True)
    world.sources = [src]
    world.obs = [_obs(src, "close", 103, AS_OF - timedelta(hours=1), symbol="AAA")]
    (widget,) = await _build(["markets"])
    assert widget.status == "empty"


async def test_markets_latest_and_change(world: SimpleNamespace) -> None:
    src = _source("econ")
    world.sources = [src]
    world.obs = [
        _obs(src, "close", 100, AS_OF - timedelta(hours=20), symbol="AAA"),
        _obs(src, "close", 103, AS_OF - timedelta(hours=1), symbol="AAA"),
    ]
    (widget,) = await _build(["markets"])
    assert widget.status == "ok"
    assert widget.items[0]["detail"] == "103 USD (+3.0% 24h)"
    assert widget.items[0]["source_id"] == str(src.id)
    assert widget.items[0]["id"] == f"{src.id}:close:AAA"


async def test_observation_widgets_are_pinned_to_as_of(world: SimpleNamespace) -> None:
    src = _source("econ")
    world.sources = [src]
    world.obs = [_obs(src, "close", 100, AS_OF - timedelta(hours=1), symbol="AAA")]
    first = await _build(["markets"])
    world.obs.append(_obs(src, "close", 200, AS_OF + timedelta(minutes=5), symbol="AAA"))
    assert await _build(["markets"]) == first


async def test_empty_widget_status(world: SimpleNamespace) -> None:
    (widget,) = await _build(["hazards"])
    assert widget.status == "empty" and widget.items == []


async def test_weather_and_hazards(world: SimpleNamespace) -> None:
    meteo, haz = _source("open_meteo"), _source("haz")
    world.sources = [meteo, haz]
    soon = AS_OF + timedelta(hours=2)
    world.obs = [
        _obs(meteo, "temperature_2m", 20, soon, unit="°C"),
        _obs(meteo, "temperature_2m", 26, soon + timedelta(hours=1), unit="°C"),
        _obs(meteo, "precipitation", 1.5, soon, unit="mm"),
        _obs(haz, "magnitude", 4.0, AS_OF - timedelta(hours=2), region="A"),
        _obs(haz, "magnitude", 6.0, AS_OF - timedelta(hours=3), region="B"),
    ]
    weather, hazards = await _build(["weather", "hazards"])
    assert weather.items[0]["detail"] == "20.0–26.0°C, rain 1.5 mm"
    assert hazards.items[0]["detail"] == "magnitude 6.0 B"


async def test_source_health_lists_only_failing_active(world: SimpleNamespace) -> None:
    t = AS_OF
    failing = _source(
        "a", last_error_at=t, last_success_at=t - timedelta(hours=1), last_error_code="timeout"
    )
    recovered = _source("b", last_error_at=t - timedelta(hours=1), last_success_at=t)
    paused = _source("c", status="paused", last_error_at=t)
    world.sources = [failing, recovered, paused]
    (widget,) = await _build(["source_health"])
    assert [i["source_id"] for i in widget.items] == [str(failing.id)]
    assert widget.items[0]["detail"] == "timeout"


def test_fact_candidates_prefers_detail() -> None:
    widget = DailyWidget(
        id="markets",
        module="dashboard",
        title_key="m",
        status="ok",
        items=[
            {"id": "1", "title": "T", "detail": "103 USD", "status": "x", "source_id": str(uuid4())}
        ],
    )
    candidates, _ = briefs._fact_candidates([widget])
    assert candidates[0]["detail"] == "103 USD"
