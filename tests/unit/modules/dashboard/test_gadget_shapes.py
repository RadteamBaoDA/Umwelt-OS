"""Shape-based renderer compatibility, compatible-source counts and the metric catalog read."""

from uuid import UUID, uuid4

import pytest

from modules.dashboard import gadgets, public
from modules.dashboard.schemas import GadgetConfiguration, GadgetScope


def test_shape_rules() -> None:
    """Sources are compatible when their data shapes intersect the renderer's accepted shapes."""
    gadgets.validate_renderer_sources("finance_chart", [("noaa_kp", "api")])
    gadgets.validate_renderer_sources("map", [("usgs", "api")])
    gadgets.validate_renderer_sources("provider_observation", [("usgs", "api")])  # legacy USGS Metric stays valid
    for renderer, source in [("telegram_feed", ("bbc_world", "rss")), ("video_panel", ("usgs", "api")),
                             ("map", ("cisa_kev", "api")), ("weather", ("noaa_kp", "api"))]:
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_renderer_sources(renderer, [source])
        assert (exc.value.code, exc.value.field) == ("source_incompatible", "source_ids")


def test_source_limit_and_sourceless() -> None:
    """Single-source renderers cap at one source; sourceless renderers reject any."""
    with pytest.raises(gadgets.GadgetConfigError) as exc:
        gadgets.validate_renderer_sources("weather", [("open_meteo", "api"), ("open_meteo", "api")])
    assert exc.value.code == "source_limit"
    with pytest.raises(gadgets.GadgetConfigError):
        gadgets.validate_renderer_sources("tasks", [("bbc_world", "rss")])
    gadgets.validate_renderer_sources("highlights", [("bbc_world", "rss"), (None, "manual")])


def test_renderer_read_exposes_shapes() -> None:
    """RendererRead carries accepted_shapes (sorted list) and None for unconstrained renderers."""
    reads = {r.id: r for r in public.renderer_reads()}
    assert reads["finance_chart"].accepted_shapes == ["series"]
    assert reads["provider_observation"].accepted_shapes == ["measurement"]
    assert reads["highlights"].accepted_shapes is None
    assert reads["news_feed"].accepted_shapes == ["event", "news"]


@pytest.mark.asyncio
async def test_compatible_counts_not_capped_by_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts come from a full active-source scan, so more than one page of sources is counted."""
    kinds = tuple((uuid4(), "noaa_kp", "api") for _ in range(150)) + ((uuid4(), "bbc_world", "rss"),)

    async def fake(*_a: object, **_k: object) -> tuple[tuple[object, str | None, str], ...]:
        return kinds

    monkeypatch.setattr(public.sources, "list_active_source_kinds", fake)
    counts = await public.compatible_source_counts(None, scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert counts["finance_chart"] == 150
    assert counts["news_feed"] == 1
    assert "tasks" not in counts


def test_metric_catalog_read_shape() -> None:
    """The catalog read serialises each provider's metrics, symbol_keyed and suggested_symbols."""
    catalog = public.metric_catalog_read()
    assert catalog
    for entry in catalog.values():
        assert set(entry) >= {"metrics", "symbol_keyed", "suggested_symbols"}
        assert all(set(m) == {"id", "unit"} for m in entry["metrics"])


def test_chart_accepts_symbolless_measurement() -> None:
    """A non-symbol-keyed provider needs no symbol and its catalog metric is accepted."""
    gadgets.validate_measurement_scope("finance_chart", "noaa_kp", GadgetScope(metrics=["kp_index"]))


def test_chart_errors_are_field_tagged() -> None:
    """Chart rejections carry the stable code and offending field the web editor maps."""
    cases = [("alpha_vantage", GadgetScope(metrics=["close"]), ("symbol_required", "symbols")),
             ("noaa_kp", GadgetScope(metrics=["close"]), ("metric_not_in_catalog", "metrics")),
             ("noaa_kp", GadgetScope(metrics=["kp_index", "x"]), ("metric_not_in_catalog", "metrics"))]
    for provider, scope, expected in cases:
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_measurement_scope("finance_chart", provider, scope)
        assert (exc.value.code, exc.value.field) == expected


def test_legacy_alpha_vantage_chart_still_valid() -> None:
    """The pre-catalog Alpha Vantage chart scope stays valid."""
    gadgets.validate_measurement_scope(
        "finance_chart", "alpha_vantage", GadgetScope(symbols=["NVDA"], metrics=["close"]))


def test_open_metric_provider_skips_catalog_membership() -> None:
    """Open-ended providers accept owner-configured metric ids; non-measurement renderers are untouched."""
    gadgets.validate_measurement_scope(
        "finance_chart", "world_bank", GadgetScope(metrics=["some_indicator"], regions=["Viet Nam"]))
    with pytest.raises(gadgets.GadgetConfigError) as exc:
        gadgets.validate_measurement_scope("finance_chart", "fred", GadgetScope(metrics=["fred_series"]))
    assert exc.value.code == "symbol_required"
    gadgets.validate_measurement_scope("news_feed", "noaa_kp", GadgetScope())


def test_metric_and_weather_rules() -> None:
    """provider_observation allows empty metrics but not unknown ones; weather uses the open_meteo catalog."""
    gadgets.validate_measurement_scope("provider_observation", "usgs", GadgetScope())
    gadgets.validate_measurement_scope("weather", "open_meteo", GadgetScope(metrics=["temperature_2m"]))
    for renderer, provider in [("provider_observation", "usgs"), ("weather", "open_meteo")]:
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_measurement_scope(renderer, provider, GadgetScope(metrics=["nope"]))
        assert (exc.value.code, exc.value.field) == ("metric_not_in_catalog", "metrics")


def test_map_layer_error_is_field_tagged() -> None:
    """An unknown map layer reports map_layer_unavailable on map_layer_ids."""
    with pytest.raises(gadgets.GadgetConfigError) as exc:
        gadgets.validate_renderer_configuration(
            "map", GadgetConfiguration(scope=GadgetScope(map_layer_ids=["bogus"])))
    assert (exc.value.code, exc.value.field) == ("map_layer_unavailable", "map_layer_ids")


class _Stop(Exception):
    """Ends create/patch right after validation, before any persistence."""


def _patch_persistence(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str]]) -> None:
    """Stub locks, admission and source lookup so create/patch run only their validation."""
    from types import SimpleNamespace

    async def noop(*_a: object, **_k: object) -> None:
        return None

    async def fetch(*_a: object, **_k: object) -> list[object]:
        return [SimpleNamespace(provider=p, type=t) for p, t in rows]

    for name in ("_admit", "_lock_owner_creation_quota", "_lock_selected_sources"):
        monkeypatch.setattr(public, name, noop)
    monkeypatch.setattr(public.sources, "get_gadget_sources", fetch)


@pytest.mark.asyncio
async def test_create_returns_field_in_422_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_definition raises symbol_required/symbols, which the route maps into 422 details.field."""
    from fastapi import HTTPException

    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard import routes
    from modules.dashboard.schemas import GadgetDefinitionCreate

    _patch_persistence(monkeypatch, [("alpha_vantage", "api")])
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    payload = GadgetDefinitionCreate(
        name="n", renderer="finance_chart", source_ids=[uuid4()], scope=GadgetScope(metrics=["close"]))
    with pytest.raises(HTTPException) as exc:
        await routes._call(public.create_definition(
            object(), payload, scope=scope, multi_workspace_enabled=False))  # type: ignore[arg-type]
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "symbol_required"
    assert exc.value.detail["details"] == {"field": "symbols"}


@pytest.mark.asyncio
async def test_patch_name_only_skips_source_and_measurement_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renaming a legacy definition does not re-run source compatibility or measurement validation."""
    from types import SimpleNamespace

    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard.schemas import GadgetDefinitionPatch

    _patch_persistence(monkeypatch, [("telegram", "telegram")])
    row = SimpleNamespace(revision=1, name="n", renderer="news_feed", source_ids=[str(uuid4())],
                          scope={}, filters={}, highlight_rules=[])

    async def lock_definition(*_a: object, **_k: object) -> object:
        return row

    class Session:
        async def scalars(self, _stmt: object) -> object:
            return SimpleNamespace(all=list)

    async def stop(*_a: object, **_k: object) -> None:
        raise _Stop

    monkeypatch.setattr(public, "_lock_definition", lock_definition)
    monkeypatch.setattr(public, "validate_highlight_rules", stop)  # reached only after source validation
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    with pytest.raises(_Stop):
        await public.patch_definition(Session(), uuid4(), GadgetDefinitionPatch(expected_revision=1, name="x"),  # type: ignore[arg-type]
                                      scope=scope, multi_workspace_enabled=False)


async def _patch_saved(monkeypatch: pytest.MonkeyPatch, provider: tuple[str, str], renderer: str,
                       stored_scope: dict, patch: dict) -> None:
    """Run patch_definition against a stored row, stopping after validation (``_Stop`` means it passed)."""
    from types import SimpleNamespace

    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard.schemas import GadgetDefinitionPatch

    _patch_persistence(monkeypatch, [provider])
    row = SimpleNamespace(revision=1, name="n", renderer=renderer, source_ids=[str(uuid4())],
                          scope=stored_scope, filters={}, highlight_rules=[])

    async def lock_definition(*_a: object, **_k: object) -> object:
        return row

    class Session:
        async def scalars(self, _stmt: object) -> object:
            return SimpleNamespace(all=list)

    async def stop(*_a: object, **_k: object) -> None:
        raise _Stop

    monkeypatch.setattr(public, "_lock_definition", lock_definition)
    monkeypatch.setattr(public, "validate_highlight_rules", stop)
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    if "scope" in patch:
        patch = {**patch, "scope": GadgetScope.model_validate(patch["scope"])}
    if "source_ids" in patch:
        patch = {**patch, "source_ids": [UUID(item) for item in row.source_ids]}
    await public.patch_definition(Session(), uuid4(), GadgetDefinitionPatch(expected_revision=1, **patch),  # type: ignore[arg-type]
                                  scope=scope, multi_workspace_enabled=False)


@pytest.mark.asyncio
async def test_patch_unchanged_scope_on_incompatible_legacy_row_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The editor resends the stored scope on rename; an incompatible saved source must not 422."""
    stored = GadgetScope(metrics=["whatever"]).model_dump(mode="json")
    with pytest.raises(_Stop):
        await _patch_saved(monkeypatch, ("github", "api"), "provider_observation", stored,
                           {"name": "x", "scope": stored})


@pytest.mark.asyncio
async def test_patch_unchanged_legacy_chart_metrics_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy chart with two metrics keeps saving while its scope is unchanged."""
    stored = GadgetScope(metrics=["close", "volume"], symbols=["NVDA"]).model_dump(mode="json")
    with pytest.raises(_Stop):
        await _patch_saved(monkeypatch, ("alpha_vantage", "api"), "finance_chart", stored,
                           {"name": "x", "scope": stored})


@pytest.mark.asyncio
async def test_patch_changed_scope_still_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the scope to a metric the provider does not emit is still rejected."""
    stored = GadgetScope(metrics=["close"], symbols=["NVDA"]).model_dump(mode="json")
    with pytest.raises(gadgets.GadgetConfigError):
        await _patch_saved(monkeypatch, ("alpha_vantage", "api"), "finance_chart", stored,
                           {"scope": {"metrics": ["not_a_metric"], "symbols": ["NVDA"]}})


def test_removed_preset_ids_alias() -> None:
    """Retired preset ids resolve to their canonical presets and are not listed."""
    assert gadgets.dashboard_preset("tech").id == "technology"
    assert gadgets.dashboard_preset("finance_variant").id == "finance"
    assert {"tech", "finance_variant"}.isdisjoint(item.id for item in gadgets.PRESETS)
    assert {"tech", "finance_variant"}.isdisjoint(item.id for item in public.preset_catalog())


def test_presets_have_no_planned_slots() -> None:
    """No preset slot may use a renderer whose runtime state is planned."""
    for preset in gadgets.PRESETS:
        for slot in preset.slots:
            assert gadgets.renderer_descriptor(slot.renderer).runtime_state == "available", (preset.id, slot.slot_id)


def test_preset_ids_unique_labels() -> None:
    """No two presets share a display label."""
    labels = [preset.label for preset in gadgets.PRESETS]
    assert len(labels) == len(set(labels))


@pytest.mark.asyncio
async def test_patch_lookback_added_over_stored_null_does_not_revalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The editor resends lookback_days over a stored null; unchanged metrics/symbols must not re-trigger the catalog rule."""
    stored = GadgetScope(metrics=["close", "volume"], symbols=["NVDA"]).model_dump(mode="json")
    resent = {**stored, "lookback_days": 90}
    with pytest.raises(_Stop):
        await _patch_saved(monkeypatch, ("alpha_vantage", "api"), "finance_chart", stored,
                           {"name": "x", "scope": resent})


@pytest.mark.asyncio
async def test_patch_legacy_metric_off_catalog_rename_with_lookback_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Metric gadget whose stored metric left the catalog still renames when only lookback differs."""
    stored = GadgetScope(metrics=["legacy_metric"]).model_dump(mode="json")
    with pytest.raises(_Stop):
        await _patch_saved(monkeypatch, ("noaa_kp", "api"), "provider_observation", stored,
                           {"name": "x", "scope": {**stored, "lookback_days": 90}})


def _preview_stubs(monkeypatch: pytest.MonkeyPatch, active: list[tuple[str | None, str]],
                   provider: str = "noaa_kp") -> None:
    """Stub admission and source reads so preview_preset only exercises its readiness rules.

    ``provider`` is the provider every explicitly selected source resolves to.
    """
    from types import SimpleNamespace

    async def admit(*_a: object, **_k: object) -> None:
        return None

    async def kinds(*_a: object, **_k: object) -> tuple[tuple[object, str | None, str], ...]:
        return tuple((uuid4(), provider, kind) for provider, kind in active)

    async def states(_s: object, ids: object, **_k: object) -> dict[UUID, object]:
        return {i: SimpleNamespace(id=i, status="active", generation=1, provider=provider, type="api")
                for i in ids}  # type: ignore[attr-defined]

    monkeypatch.setattr(public, "_admit", admit)
    monkeypatch.setattr(public, "_source_states", states)
    monkeypatch.setattr(public.sources, "list_active_source_kinds", kinds)


def _codes(preview: object, slot_id: str) -> list[str]:
    """Return warning codes of one previewed slot."""
    slot = next(s for s in preview.slots if s.slot_id == slot_id)  # type: ignore[attr-defined]
    return [w.code for w in slot.warnings]


@pytest.mark.asyncio
async def test_personal_preset_previews_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sourceless renderers (tasks/goals/daily_brief) never warn missing_source, even with no sources at all."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [])
    preview = await public.preview_preset(
        None, "personal", PresetPreviewRequest(slot_sources={}), scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    for slot_id in ("tasks", "goals", "daily_brief"):
        assert _codes(preview, slot_id) == [], slot_id


@pytest.mark.asyncio
async def test_finance_preset_ready_with_compatible_source_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compatible active source exists but is unbound: the chart slot warns source_unbound (not ready); news needs setup."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [("noaa_kp", "api")])
    preview = await public.preview_preset(
        None, "finance", PresetPreviewRequest(slot_sources={}), scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert _codes(preview, "finance_chart") == ["source_unbound"]
    assert _codes(preview, "news_feed") == ["missing_source"]


@pytest.mark.asyncio
async def test_preview_bound_slot_ready_with_seeded_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly bound compatible single source: no warning, scope seeded from the catalog."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [("noaa_kp", "api")])
    preview = await public.preview_preset(
        None, "finance", PresetPreviewRequest(slot_sources={"finance_chart": [uuid4()]}),
        scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    chart = next(s for s in preview.slots if s.slot_id == "finance_chart")  # type: ignore[attr-defined]
    assert chart.warnings == [] and chart.scope == {"metrics": ["kp_index"], "lookback_days": 30}


@pytest.mark.asyncio
async def test_finance_preset_needs_setup_without_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no compatible active source the source slots report missing_source with the data_sources setup group."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [])
    preview = await public.preview_preset(
        None, "finance", PresetPreviewRequest(slot_sources={}), scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    slot = next(s for s in preview.slots if s.slot_id == "finance_chart")  # type: ignore[attr-defined]
    assert [(w.code, w.setup_group) for w in slot.warnings] == [("missing_source", "data_sources")]


@pytest.mark.asyncio
async def test_preview_warns_incompatible_explicit_source_and_defaults_chart_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit sources failing the shape rule warn source_incompatible; a single measurement source seeds the chart scope."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [("noaa_kp", "api")])
    source_id = uuid4()
    preview = await public.preview_preset(
        None, "finance", PresetPreviewRequest(slot_sources={"finance_chart": [source_id], "news_feed": [source_id]}),
        scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    chart = next(s for s in preview.slots if s.slot_id == "finance_chart")  # type: ignore[attr-defined]
    assert chart.warnings == [] and chart.scope == {"metrics": ["kp_index"], "lookback_days": 30}
    assert _codes(preview, "news_feed") == ["source_incompatible"]


def test_preset_default_scope_uses_catalog() -> None:
    """Chart defaults: first catalog metric, first suggested symbol when symbol-keyed, empty when unbound."""
    assert gadgets.preset_slot_scope("finance_chart", "alpha_vantage") == {}  # symbol-keyed, no suggestions
    # Owner-configured symbols (fred series, binance pairs) are never seeded from the static suggestion.
    assert gadgets.preset_slot_scope("finance_chart", "fred") == {}
    assert gadgets.preset_slot_scope("finance_chart", "binance") == {}
    assert gadgets.preset_slot_scope("finance_chart", "coinpaprika") == {"metrics": ["spot_price"], "symbols": ["BTC"], "lookback_days": 3}
    assert gadgets.preset_slot_scope("finance_chart", None) == {}
    assert gadgets.preset_slot_scope("news_feed", "noaa_kp") == {}


def test_usgs_is_not_chart_compatible_but_metric_compatible() -> None:
    """Point-event providers (quakes, hotspots) are not a series: charts reject them, Metric keeps them."""
    for provider in ("usgs", "nasa_firms"):
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_renderer_sources("finance_chart", [(provider, "api")])
        assert (exc.value.code, exc.value.field) == ("source_incompatible", "source_ids")
        gadgets.validate_renderer_sources("provider_observation", [(provider, "api")])
    gadgets.validate_renderer_sources("finance_chart", [("world_bank", "api")])


@pytest.mark.asyncio
async def test_compatible_counts_exclude_usgs_for_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    """usgs counts for provider_observation and table_panel but not for finance_chart."""
    kinds = ((uuid4(), "usgs", "api"), (uuid4(), "noaa_kp", "api"))

    async def fake(*_a: object, **_k: object) -> tuple[tuple[object, str | None, str], ...]:
        return kinds

    monkeypatch.setattr(public.sources, "list_active_source_kinds", fake)
    counts = await public.compatible_source_counts(None, scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert counts["finance_chart"] == 1
    assert counts["provider_observation"] == 2


def test_preset_slot_scope_never_seeds_usgs_chart() -> None:
    """A usgs-bound chart slot seeds nothing; world_bank (single country, not region keyed) seeds only a metric."""
    assert gadgets.preset_slot_scope("finance_chart", "usgs") == {}
    assert gadgets.preset_slot_scope("finance_chart", "world_bank") == {
        "metrics": ["gdp_current_usd"], "lookback_days": 3650}
    assert gadgets.preset_slot_scope("finance_chart", "oecd_cli")["lookback_days"] == 730
    assert gadgets.preset_slot_scope("provider_observation", "usgs") == {"metrics": ["earthquake_magnitude"]}
    # A Metric slot never starts narrower than its 30-day runtime window; the chart keeps the 3-day catalog default.
    assert gadgets.preset_slot_scope("provider_observation", "coinpaprika")["lookback_days"] == 30
    assert gadgets.preset_slot_scope("provider_observation", "world_bank")["lookback_days"] == 3650


def test_region_keyed_chart_needs_region() -> None:
    """Region-keyed providers need one region (field regions); symbol-keyed rules are unchanged."""
    with pytest.raises(gadgets.GadgetConfigError) as exc:
        gadgets.validate_measurement_scope("finance_chart", "openaq", GadgetScope(metrics=["pm25"]))
    assert (exc.value.code, exc.value.field) == ("region_required", "regions")
    gadgets.validate_measurement_scope("finance_chart", "openaq", GadgetScope(metrics=["pm25"], regions=["location 8118"]))
    gadgets.validate_renderer_configuration("finance_chart", GadgetConfiguration(scope=GadgetScope(regions=["x"])))


def test_default_metric_rule() -> None:
    """close when listed, else the first metric."""
    assert gadgets.default_metric(["open", "close"]) == "close"
    assert gadgets.default_metric(["kp_index"]) == "kp_index"


@pytest.mark.asyncio
async def test_sourceless_chart_and_weather_keep_metric_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no bound source, finance_chart uses the Alpha Vantage and weather the Open-Meteo metric allowlist."""
    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard.schemas import GadgetDefinitionCreate

    _patch_persistence(monkeypatch, [])
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    for renderer, metric in (("finance_chart", "nope"), ("weather", "nope")):
        payload = GadgetDefinitionCreate(name="n", renderer=renderer, source_ids=[], scope=GadgetScope(metrics=[metric]))
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            await public.create_definition(object(), payload, scope=scope, multi_workspace_enabled=False)  # type: ignore[arg-type]
        assert exc.value.code == "metric_not_in_catalog", renderer


@pytest.mark.asyncio
async def test_preset_apply_rejects_incompatible_foreign_and_archived_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """slot_sources ids that are incompatible, foreign/unknown or archived never apply."""
    from types import SimpleNamespace

    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard.schemas import PresetApplyRequest

    async def noop(*_a: object, **_k: object) -> object:
        return SimpleNamespace()

    state: dict[str, object] = {"lock": "active", "provider": "noaa_kp"}

    async def lock_source(*_a: object, **_k: object) -> object:
        return None if state["lock"] == "foreign" else SimpleNamespace(status=state["lock"])

    async def get_sources(*_a: object, **_k: object) -> list[object]:
        return [SimpleNamespace(provider=state["provider"], type="api")]

    for name in ("_admit", "_lock_owner_creation_quota"):
        monkeypatch.setattr(public, name, noop)
    monkeypatch.setattr(public.sources, "lock_source", lock_source)
    monkeypatch.setattr(public.sources, "get_gadget_sources", get_sources)
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    payload = PresetApplyRequest(
        slot_sources={"finance_chart": [uuid4()]}, preview_fingerprint="0" * 64, mode="create")

    async def apply() -> None:
        await public.apply_preset(object(), "finance", payload, scope=scope, multi_workspace_enabled=False)  # type: ignore[arg-type]

    state["lock"] = "foreign"
    with pytest.raises(public.DashboardConflict) as conflict:
        await apply()
    assert conflict.value.code == "source_changed"
    state["lock"] = "archived"
    with pytest.raises(public.DashboardConflict) as conflict:
        await apply()
    assert conflict.value.code == "source_changed"
    state.update(lock="active", provider="usgs")
    with pytest.raises(gadgets.GadgetConfigError) as incompatible:
        await apply()
    assert incompatible.value.code == "source_incompatible"


class _ScopedSourceSession:
    """Fake session whose ``scalar`` evaluates the real lock query's bound parameters against in-memory Source rows.

    A row is returned only when the statement binds both its id and its workspace id, so the workspace predicate built by
    ``sources.public._source_scope`` is what decides visibility (no stubbed ``lock_source`` result).
    """

    def __init__(self, rows: list[object]) -> None:
        """Keep the rows the fake database holds."""
        self.rows = rows

    async def scalar(self, statement: object) -> object | None:
        """Return the first row matching the statement's bound id and workspace id, else None."""
        values = list(statement.compile().params.values())  # type: ignore[attr-defined]
        return next((row for row in self.rows if row.id in values and row.workspace_id in values), None)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_preset_apply_foreign_workspace_source_is_unavailable_through_real_lock_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source row of another workspace is invisible to the real ``lock_source`` query and applies as source_changed."""
    from types import SimpleNamespace

    from core.workspaces.schemas import WorkspaceContext
    from modules.dashboard.schemas import PresetApplyRequest
    from modules.sources import public as sources_public

    async def noop(*_a: object, **_k: object) -> object:
        return SimpleNamespace()

    async def get_sources(*_a: object, **_k: object) -> list[object]:
        return [SimpleNamespace(provider="noaa_kp", type="api")]

    for name in ("_admit", "_lock_owner_creation_quota"):
        monkeypatch.setattr(public, name, noop)
    monkeypatch.setattr(sources_public, "_admit_source_scope", noop)  # admission is not under test; the row query is
    monkeypatch.setattr(public.sources, "get_gadget_sources", get_sources)
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)
    source_id = uuid4()

    def row(workspace_id: object) -> SimpleNamespace:
        """In-memory Source row with the lifecycle fields ``lock_source`` projects."""
        return SimpleNamespace(id=source_id, workspace_id=workspace_id, status="active", generation=1, local_only=False,
                               graph_enabled=True, rag_index_enabled=True)

    payload = PresetApplyRequest(slot_sources={"finance_chart": [source_id]}, preview_fingerprint="0" * 64, mode="create")

    async def apply(rows: list[object]) -> None:
        await public.apply_preset(_ScopedSourceSession(rows), "finance", payload, scope=scope,  # type: ignore[arg-type]
                                  multi_workspace_enabled=False)

    with pytest.raises(public.DashboardConflict) as conflict:
        await apply([row(uuid4())])  # same id, other workspace
    assert conflict.value.code == "source_changed"
    # Positive control: the same row in the caller's workspace passes the lock and fails later (not at source_changed).
    with pytest.raises(Exception) as later:
        await apply([row(scope.workspace_id)])
    assert getattr(later.value, "code", None) != "source_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "codes"), [("alpha_vantage", ["scope_required"]),
                                                 ("fred", ["scope_required"]), ("binance", ["scope_required"]),
                                                 ("openaq", ["scope_required"]), ("noaa_kp", [])])
async def test_preview_bound_chart_needing_scope_is_not_ready(
        monkeypatch: pytest.MonkeyPatch, provider: str, codes: list[str]) -> None:
    """Symbol/region-keyed bound chart sources without a suggested key warn scope_required (data_sources)."""
    from modules.dashboard.schemas import PresetPreviewRequest

    _preview_stubs(monkeypatch, [(provider, "api")], provider=provider)
    preview = await public.preview_preset(
        None, "finance", PresetPreviewRequest(slot_sources={"finance_chart": [uuid4()]}),
        scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    chart = next(s for s in preview.slots if s.slot_id == "finance_chart")  # type: ignore[attr-defined]
    assert [w.code for w in chart.warnings] == codes
    assert all(w.setup_group == "data_sources" for w in chart.warnings)


@pytest.mark.asyncio
async def test_preview_scope_required_only_for_finance_chart_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Metric (provider_observation) slot bound to a symbol-keyed provider needs no symbol, so no scope_required."""
    from modules.dashboard import gadgets
    from modules.dashboard.schemas import PresetPreviewRequest

    preset = gadgets.DashboardPreset("t_metric", "T", "variant", (
        gadgets.PresetSlot("chart", "finance_chart"), gadgets.PresetSlot("metric", "provider_observation")))
    monkeypatch.setitem(gadgets._PRESETS_BY_ID, "t_metric", preset)
    _preview_stubs(monkeypatch, [("binance", "api")], provider="binance")
    preview = await public.preview_preset(
        None, "t_metric", PresetPreviewRequest(slot_sources={"chart": [uuid4()], "metric": [uuid4()]}),
        scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert _codes(preview, "chart") == ["scope_required"]
    assert _codes(preview, "metric") == []


def test_preset_definition_names_use_display_names() -> None:
    """Preset-created definition names use the en-us renderer display name, not the raw renderer id."""
    assert public.preset_definition_name("Finance", "finance_chart") == "Finance · Chart"
    assert public.preset_definition_name("Overview", "news_feed") == "Overview · Feed"
    assert public.preset_definition_name("Overview", "map") == "Overview · Map"
    assert public.preset_definition_name("X", "unknown_thing") == "X · unknown_thing"
