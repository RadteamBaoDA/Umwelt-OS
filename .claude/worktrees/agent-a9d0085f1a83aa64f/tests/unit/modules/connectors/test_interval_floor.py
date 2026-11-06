"""Hard per-provider minimum collection interval: table, save-time 422, enable default, cooldown, catalog exposure."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import builtins, registry, routes
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.catalog import get_catalog_entry
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS,
    FREE_PROVIDER_SPECS,
    PROVIDER_MIN_INTERVAL_MINUTES,
    collection_cooldown_seconds,
    min_interval_minutes,
)
from modules.sources.schemas import ConnectorSource
from tests.unit.modules.connectors.test_builtins import SCOPE, _enable, _request, _session, _World

# Providers that deliberately keep the 15-minute default: real-time prices/hazards and owner-credential sources.
# A new provider must be added either here or to PROVIDER_MIN_INTERVAL_MINUTES, so the choice is conscious.
FIFTEEN_MINUTE_PROVIDERS = frozenset(
    {
        "binance",
        "coinpaprika",
        "coingecko",
        "iss_position",
        "nws_alerts",
        "usgs",
        "hn_top",
        "nasa_eonet",
        "github",
        "reddit",
        "google_mail",
        "google_calendar",
        "google_drive",
        "notion",
        "slack",
        "telegram",
        "home_assistant",
        "health",
        "mastodon",
        "x",
    }
)
# Floors pre-registered for a provider with no adapter yet (none: ITviec shipped on the owner's 2026-11-04 decision).
UNREGISTERED_FLOORS: frozenset[str] = frozenset()


def test_every_provider_has_a_conscious_floor() -> None:
    """Each registered provider is in the floor table or in the explicit 15-minute set, never both."""
    providers = set(PROVIDER_SOURCE_TYPES)
    floored = set(PROVIDER_MIN_INTERVAL_MINUTES)
    assert providers - floored - FIFTEEN_MINUTE_PROVIDERS == set()
    assert not floored & FIFTEEN_MINUTE_PROVIDERS
    assert floored - providers == UNREGISTERED_FLOORS
    assert FIFTEEN_MINUTE_PROVIDERS <= providers


def test_floors_are_selectable_intervals_and_spec_defaults_respect_them() -> None:
    """Floors are values the interval Literal accepts; no spec default sits below its floor."""
    assert set(PROVIDER_MIN_INTERVAL_MINUTES.values()) <= {15, 30, 60, 360, 1440}
    for spec in FREE_PROVIDER_SPECS:
        assert spec.default_interval_minutes >= min_interval_minutes(spec.id), spec.id


def test_new_unofficial_providers_are_six_hours() -> None:
    """Owner rule: unofficial/scraped and job sources poll at most every 6 hours."""
    for provider in ("vn_stock", "github_trending", "wwr_jobs", "vietnamworks_jobs", "itviec_jobs"):
        assert min_interval_minutes(provider) == 360
    assert min_interval_minutes("alpha_vantage") == 1440 and min_interval_minutes("rss") == 15


def _source(provider: str, interval: int | None) -> ConnectorSource:
    """Active built-in source row with an optional explicit interval."""
    cfg: dict[str, object] = {"history_mode": "returned_snapshot", "timeout_seconds": 30}
    if interval is not None:
        cfg["schedule_interval_minutes"] = interval
    return ConnectorSource(
        id=uuid4(),
        workspace_id=uuid4(),
        type=PROVIDER_SOURCE_TYPES[provider],
        status="active",
        generation=1,
        configuration=cfg,
        local_only=False,
        provider=provider,
    )


def test_registry_rejects_interval_below_floor_and_accepts_floor() -> None:
    """Save/validate-draft/validate all pass through registry.validate."""
    with pytest.raises(ValueError, match="schedule_interval_below_minimum: minimum 360"):
        registry.validate(_source("vn_stock", 60))
    registry.validate(_source("vn_stock", 360))
    registry.validate(_source("vn_stock", 1440))
    with pytest.raises(ValueError, match="schedule_interval_below_minimum: minimum 1440"):
        registry.validate(_source("world_bank", 360))
    registry.validate(_source("binance", 15))


def test_omitted_interval_defaults_to_the_floor_for_slow_providers() -> None:
    """A saved config with no interval is clamped up to the floor, not rejected."""
    assert registry.configuration(_source("vn_stock", None)).schedule_interval_minutes == 360
    assert registry.configuration(_source("alpha_vantage", None)).schedule_interval_minutes == 1440


async def test_enable_defaults_to_floor_and_rejects_shorter_interval() -> None:
    """Built-in enable: default = max(spec default, floor); explicit shorter value is a 422 with the minimum."""
    with _World("vn_stock") as world:
        await _enable("vn_stock")
    assert world.row.desired_configuration["schedule_interval_minutes"] == 360
    with _World("vn_stock"), pytest.raises(HTTPException) as exc:
        await builtins.enable_builtin(
            "vn_stock",
            _session(),
            _request(),
            SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(schedule_interval_minutes=60),
        )
    assert exc.value.status_code == 422 and "schedule_interval_below_minimum: minimum 360" in str(
        exc.value.detail
    )


async def test_builtin_read_and_catalog_expose_the_floor() -> None:
    """The UI reads min_interval_minutes from both the built-in rows and the catalog entries."""
    with _World("vn_stock"):
        read = await _enable("vn_stock")
    assert read.min_interval_minutes == 360
    entry = get_catalog_entry("wwr_jobs")
    assert entry is not None and entry.min_interval_minutes == 360
    assert BUILTIN_PROVIDERS >= {"vn_stock", "github_trending", "wwr_jobs"}


def test_manual_collect_cooldown_only_for_slow_providers() -> None:
    """Floor >= 6 h blocks Collect now until the floor passes; faster providers and never-run sources are free."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert collection_cooldown_seconds("vn_stock", now - timedelta(hours=1), now) == 5 * 3600
    assert collection_cooldown_seconds("vn_stock", now - timedelta(hours=6), now) == 0
    assert collection_cooldown_seconds("vn_stock", None, now) == 0
    assert collection_cooldown_seconds("lobsters", now - timedelta(minutes=1), now) == 0
    assert collection_cooldown_seconds(None, now, now) == 0


def _collect_env(
    monkeypatch: pytest.MonkeyPatch, provider: str, last_started: datetime | None
) -> tuple[MagicMock, AsyncMock]:
    """Patch the collect-now route's collaborators for a native, fenced source whose last start is ``last_started``."""
    session = MagicMock(rollback=AsyncMock(), scalar=AsyncMock(return_value=last_started))
    queued = AsyncMock(return_value=SimpleNamespace(status="queued", request_id=uuid4()))
    source = SimpleNamespace(type="api", status="active", provider=provider, generation=1)
    monkeypatch.setattr(routes, "_owner_access", AsyncMock())
    monkeypatch.setattr(routes, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        routes.provisioning,
        "activation_status",
        AsyncMock(
            return_value=SimpleNamespace(
                state="active",
                applied_revision=2,
                desired_revision=2,
                source_generation=1,
                execution_backend="native",
            )
        ),
    )
    monkeypatch.setattr(
        routes.provisioning, "require_collection_fence", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(routes.scheduler, "request_collection", queued)
    return session, queued


async def _collect(session: MagicMock) -> object:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))
        )
    )
    return await routes.trigger_collection(
        uuid4(), session, request, SimpleNamespace(workspace_id=uuid4())
    )  # type: ignore[arg-type]


async def test_collect_now_inside_a_six_hour_floor_is_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A floor-360 provider started 1 h ago answers 429 `collection_too_soon` with the remaining seconds, queueing nothing."""
    session, queued = _collect_env(
        monkeypatch, "vietnamworks_jobs", datetime.now(UTC) - timedelta(hours=1)
    )
    with pytest.raises(HTTPException) as exc:
        await _collect(session)
    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "collection_too_soon"  # type: ignore[index]
    assert 5 * 3600 - 60 <= exc.value.detail["retry_after"] <= 5 * 3600  # type: ignore[index]
    assert exc.value.headers == {"Retry-After": str(exc.value.detail["retry_after"])}  # type: ignore[index]
    queued.assert_not_awaited()


async def test_collect_now_after_the_floor_or_for_fast_providers_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the floor, or for a floor-15 provider started a minute ago, the manual run is queued as before."""
    session, queued = _collect_env(
        monkeypatch, "vietnamworks_jobs", datetime.now(UTC) - timedelta(hours=7)
    )
    assert (await _collect(session)).status == "queued"  # type: ignore[attr-defined]
    session, queued = _collect_env(monkeypatch, "binance", datetime.now(UTC) - timedelta(minutes=1))
    assert (await _collect(session)).status == "queued"  # type: ignore[attr-defined]
    queued.assert_awaited_once()
    session.scalar.assert_not_awaited()  # the last-start lookup is skipped for floor < 6 h
