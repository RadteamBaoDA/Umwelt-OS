# Gadget Template Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display-first gadget creation where every template, datasource, preset slot and Sources link is filtered by the data shape a source produces, Chart draws any measurement series, and no step leads to a dead end.

**Architecture:** Connectors own a static per-provider *data shape* table and *metric catalog* (`modules/connectors/data_shapes.py`, `modules/connectors/metric_catalog.py`), exposed through `modules/connectors/public.py`. Dashboard renderers declare accepted shapes instead of provider allowlists; dashboard validates sources/metrics against those connector contracts and returns field-tagged 422s. The web editor, preset picker and Settings → Sources read the same shapes over HTTP.

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy async (pytest), Next.js App Router + React Query + next-intl + shadcn/ui (Vitest + Testing Library).

**Spec:** `docs/superpowers/specs/2026-10-29-gadget-template-flow-design.md`

## Global Constraints

- Dashboard reads sources only via `modules/sources/public.py`; connector facts only via `modules/connectors/public.py`. Never import connector internals from `modules/dashboard`.
- Backend must enforce the same compatibility rule the UI shows ("Frontend-only compatibility" is an anti-goal).
- Saved definitions whose source is now incompatible still render and still allow unrelated edits; `source_ids` is sent on PATCH only when changed (keep `gadget-editor.tsx` behaviour).
- Never add connector configuration, credentials or content to `GadgetSourceSelection`.
- UI: shadcn primitives from `apps/web/src/components/ui`, semantic tokens only, every new string in both `en-us` and `vi-vi` message objects.
- Every new/changed named production function/component has JSDoc (TS) or a docstring (Python) per `AGENTS.md` "Mandatory Code Documentation".
- Run `gitnexus_impact({target, direction: "upstream"})` before editing each existing symbol named in a task; report HIGH/CRITICAL before editing.
- Commit steps run only once the owner has approved committing in this session (repo rule: commit only when asked). Otherwise leave changes staged and continue.
- Python checks: `.venv/Scripts/python.exe -m pytest <path> -q`, `ruff check`, `mypy` on touched modules. Web checks from `apps/web`: `npx vitest run <file>`, `npx tsc --noEmit`, `npx eslint <files>`. No Docker e2e without owner approval.

## Review Focus

1. **Workspace with >100 sources** — tile counts and picker must not silently undercount; counts come from the server (Task 3 test `test_compatible_counts_not_capped_by_page`), picker pages with `next_cursor` (Task 6 test `loads next source page`).
2. **Saved legacy gadget** (Alpha Vantage chart with `symbols=['NVDA'], metrics=['close']`, or a news_feed bound to a now-incompatible source) — still renders and a rename-only PATCH succeeds (Task 4 test `test_patch_name_only_keeps_incompatible_source`).
3. **Measurement provider emitting a metric missing from the catalog** — save must not accept a metric the adapter never emits, and every builtin measurement provider has a catalog entry (Task 2 test `test_every_measurement_provider_has_catalog_entry`).
4. **Stale preset id in an open browser** (`tech`, `finance_variant`) — preview/apply still resolve via alias (Task 9 test `test_removed_preset_ids_alias`).
5. **`?shapes=` with unknown or empty values on Settings → Sources** — page ignores unknown shapes and shows the unfiltered list instead of an empty one (Task 8 test `ignores unknown shapes`).

---

## File Structure

| File | Responsibility |
|---|---|
| `modules/connectors/data_shapes.py` (new) | `DataShape` literal, per-provider/source-type shape table, `source_data_shapes()` |
| `modules/connectors/metric_catalog.py` (new) | Static `METRIC_CATALOG` per measurement provider, `provider_metrics()` |
| `modules/connectors/public.py` | Re-export the two lookups for other modules |
| `modules/connectors/catalog.py`, `modules/connectors/builtins.py` | Add `data_shapes` to `CatalogEntry` / `BuiltinSourceRead` |
| `modules/dashboard/gadgets.py` | Renderer `accepted_shapes`, shape-based `validate_renderer_sources`, `GadgetConfigError`, measurement scope validation, preset cleanup + aliases |
| `modules/dashboard/public.py`, `routes.py`, `schemas.py` | Shape-aware source validation, compatible counts, metric catalog read, field-tagged 422 |
| `modules/sources/public.py`, `schemas.py` | `last_success_at` on `GadgetSourceSelection`; active-source scan for counts |
| `apps/web/src/modules/dashboard/api.ts` | Types + fetchers for shapes, counts, metric catalog |
| `apps/web/src/modules/dashboard/gadget-editor.tsx` | Tiles with counts/links, shape-filtered picker, renderer-relevant fields, pickers, field errors |
| `apps/web/src/modules/dashboard/gadgets/finance-chart.tsx` | Symbol optional when provider is not symbol-keyed |
| `apps/web/src/modules/dashboard/layout-editor.tsx` | Drop instance-title field |
| `apps/web/src/modules/dashboard/preset-picker.tsx` | Localized slots, no fingerprint/cols, shape links, Cancel variant |
| `apps/web/src/modules/dashboard/sources-link.ts` (new) | `sourcesLinkFor(renderer)` shared by editor and preset picker |
| `apps/web/src/modules/sources/source-list.tsx`, `builtin-sources.tsx`, `connector-editor.tsx` | `?shapes=&for=` filter banner and filtered add grids |
| `apps/web/src/core/messages/{gadget-settings,dashboard,sources}.ts` | New copy; remove dead `gadgetSettings.templates.*` |

---

### Task 1: Connector data shapes

**Files:**
- Create: `modules/connectors/data_shapes.py`
- Modify: `modules/connectors/public.py`, `modules/connectors/catalog.py:19-60` (`CatalogEntry`), `modules/connectors/builtins.py:55-100` (`BuiltinSourceRead` + its builder)
- Test: `tests/unit/modules/connectors/test_data_shapes.py`

**Interfaces:**
- Produces: `DataShape = Literal["news", "measurement", "event", "located", "messages", "video", "project_activity", "weather"]`; `source_data_shapes(provider: str | None, source_type: str) -> frozenset[DataShape]` (re-exported from `modules.connectors.public`); `CatalogEntry.data_shapes: list[DataShape]`; `BuiltinSourceRead.data_shapes: list[DataShape]`.

Rules the table must encode:
- Catalog providers start from `FreeProviderSpec.data_kind` (`news|measurement|event`).
- `located` is added only for providers whose adapter writes observations with non-None `latitude`/`longitude` (verify by reading each adapter under `modules/connectors/providers/`). Must include `usgs`; must exclude `cisa_kev`, `nvd`, `launch_library`, `ioda`, `us_travel_advisories`, `sec_edgar`, `openalex`.
- Extra shapes: `usgs` also `measurement` (it emits `earthquake_magnitude`); other quake feeds likewise if they emit a numeric magnitude.
- Non-catalog providers: `telegram`→`messages`; `youtube`→`video`; `github`→`project_activity`; `open_meteo`→`measurement`,`weather`; `alpha_vantage`→`measurement` (from spec); `reddit`, `mastodon`, `x`, `bluesky`, `github_releases`, `arxiv`, `huggingface`, `google_*`, `notion`, `slack` → `news`. `health`, `home_assistant` → `measurement` only if their adapters write observations, else `news`.
- `provider is None` (custom RSS/web/REST/manual) → `news`.

- [ ] **Step 1: Write failing tests**

```python
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.public import source_data_shapes

def test_every_known_provider_has_shapes() -> None:
    for provider, source_type in PROVIDER_SOURCE_TYPES.items():
        assert source_data_shapes(provider, source_type), provider

def test_location_is_separate_from_event() -> None:
    assert {"event", "located", "measurement"} <= source_data_shapes("usgs", "api")
    for provider in ("cisa_kev", "nvd", "launch_library", "ioda", "us_travel_advisories"):
        assert "located" not in source_data_shapes(provider, "api")

def test_non_catalog_shapes() -> None:
    assert source_data_shapes("telegram", "api") == {"messages"}
    assert source_data_shapes("youtube", "rss") == {"video"}
    assert source_data_shapes("github", "api") == {"project_activity"}
    assert {"measurement", "weather"} <= source_data_shapes("open_meteo", "api")
    assert source_data_shapes(None, "rss") == {"news"}
    assert source_data_shapes("bbc_world", "rss") == {"news"}

def test_catalog_and_builtin_reads_carry_shapes() -> None:
    from modules.connectors import catalog
    entry = catalog.get_catalog_entry("noaa_kp")
    assert entry is not None and "measurement" in entry.data_shapes
```
Plus one assertion in `tests/unit/modules/connectors/test_builtins.py` that a serialized built-in row includes `data_shapes`.

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/unit/modules/connectors/test_data_shapes.py -q` → FAIL (import error).
- [ ] **Step 3: Implement** `data_shapes.py` (module-level frozen mappings + `source_data_shapes`), re-export in `public.py`, add `data_shapes` field to `CatalogEntry` and `BuiltinSourceRead` and fill it where they are built (`list_catalog()` / builtin serializer).
- [ ] **Step 4: Run** the new test file and `tests/unit/modules/connectors/test_builtins.py tests/unit/modules/connectors/test_public.py` → PASS.
- [ ] **Step 5: Commit** `feat(connectors): declare per-provider data shapes`

---

### Task 2: Metric catalog

**Files:**
- Create: `modules/connectors/metric_catalog.py`
- Modify: `modules/connectors/public.py`
- Test: `tests/unit/modules/connectors/test_metric_catalog.py`

**Interfaces:**
- Consumes: `source_data_shapes` (Task 1).
- Produces (re-exported from `modules.connectors.public`):
  ```python
  @dataclass(frozen=True, slots=True)
  class MetricInfo:
      id: str
      unit: str | None

  @dataclass(frozen=True, slots=True)
  class ProviderMetrics:
      metrics: tuple[MetricInfo, ...]
      symbol_keyed: bool
      suggested_symbols: tuple[str, ...] = ()

  def provider_metrics(provider: str | None) -> ProviderMetrics | None: ...
  def metric_catalog() -> Mapping[str, ProviderMetrics]: ...
  ```

Pinned entries (metric ids are what the adapters already emit; implementer verifies each against its adapter and fills the rest):
- `alpha_vantage`: `open, high, low, close, volume`, symbol_keyed=True
- `open_meteo`: `temperature_2m, relative_humidity_2m, precipitation, wind_speed_10m`, symbol_keyed=False
- `alternative_me`: `fear_greed_index`
- `binance`, `coinpaprika`, `coingecko`: `spot_price`, symbol_keyed=True, suggested `("BTC",)` plus whatever symbols the adapters emit
- `frankfurter`, `ecb`, `open_er_api`: `fx_reference_rate`, symbol_keyed=True, suggested from the pairs the adapters emit (e.g. `EUR/USD`, `USD/VND`)
- `noaa_kp`: `kp_index`; `oecd_cli`: `composite_leading_indicator`; `iss_position`: `iss_altitude`; `usgs`: `earthquake_magnitude`; `disease_sh`: the `covid19_*` metrics it emits
- `world_bank`: metric ids from its configured indicator set in `providers/macro.py`

- [ ] **Step 1: Write failing tests**

```python
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.public import metric_catalog, provider_metrics, source_data_shapes

def test_every_measurement_provider_has_catalog_entry() -> None:
    for provider, source_type in PROVIDER_SOURCE_TYPES.items():
        if "measurement" in source_data_shapes(provider, source_type):
            entry = provider_metrics(provider)
            assert entry is not None and entry.metrics, provider

def test_pinned_entries() -> None:
    assert [m.id for m in provider_metrics("alpha_vantage").metrics] == ["open", "high", "low", "close", "volume"]
    assert provider_metrics("alpha_vantage").symbol_keyed
    assert not provider_metrics("noaa_kp").symbol_keyed
    assert [m.id for m in provider_metrics("noaa_kp").metrics] == ["kp_index"]
    assert provider_metrics("bbc_world") is None and provider_metrics(None) is None

def test_ids_unique_per_provider() -> None:
    for provider, entry in metric_catalog().items():
        ids = [m.id for m in entry.metrics]
        assert len(ids) == len(set(ids)), provider
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the catalog as one module-level `MappingProxyType`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(connectors): add static per-provider metric catalog`

---

### Task 3: Renderer shapes, source validation, counts and catalog reads (backend)

**Files:**
- Modify: `modules/dashboard/gadgets.py` (`RendererDescriptor`, `RENDERERS`, `GadgetTemplate`, `TEMPLATES`, `validate_renderer_sources`, `_check_templates`)
- Modify: `modules/dashboard/schemas.py:493-517` (`RendererRead`, `GadgetTemplateRead`)
- Modify: `modules/dashboard/public.py:1819-1858` (`renderer_reads`, `list_templates`, `_validate_selected_sources`), add `compatible_source_counts`, `metric_catalog_read`
- Modify: `modules/dashboard/routes.py:270-300` (new GETs)
- Modify: `modules/sources/public.py:1213` (`list_gadget_sources` adds `last_success_at`), add `list_active_source_kinds`; `modules/sources/schemas.py:155` (`GadgetSourceSelection.last_success_at: datetime | None`)
- Test: `tests/unit/modules/dashboard/test_gadgets_templates.py` (rewrite stale `fear_greed` assertions), `tests/unit/modules/dashboard/test_gadget_shapes.py` (new)

**Interfaces:**
- Consumes: `source_data_shapes`, `metric_catalog` (Tasks 1–2).
- Produces:
  - `RendererDescriptor.accepted_shapes: frozenset[DataShape] | None` (None = keep current any-source behaviour); `source_providers` field removed.
  - `RendererRead.accepted_shapes: list[str] | None` (replaces `source_providers`); `GadgetTemplateRead` drops `providers`, adds `accepted_shapes: list[str] | None`.
  - `validate_renderer_sources(renderer_id: str, sources: Sequence[tuple[str | None, str]]) -> None` — each tuple is `(provider, source_type)`; raises `GadgetConfigError(code="source_incompatible", field="source_ids")` / `code="source_limit"`.
  - `sources.list_active_source_kinds(session, *, scope, multi_workspace_enabled) -> tuple[tuple[UUID, str | None, str], ...]` — every active source `(id, provider, type)` in scope; `# ponytail: full scan of active sources, single-user workspace; page if counts get slow`.
  - `GET /api/v1/gadget-renderers/compatible-counts` → `dict[str, int]` (renderer id → active compatible sources; renderers with `source_mode="none"` omitted).
  - `GET /api/v1/gadget-metric-catalog` → `dict[str, {"metrics": [{"id", "unit"}], "symbol_keyed": bool, "suggested_symbols": [str]}]`, cacheable like `/gadget-templates`.
  - `GET /api/v1/gadget-sources` now returns dashboard-owned `GadgetSourceOptionPage` (in `modules/dashboard/schemas.py`): each item = all `GadgetSourceSelection` fields + `last_success_at: datetime | None` + `data_shapes: list[str]` filled via `source_data_shapes(provider, type)`. Shapes are added in dashboard, not in `modules/sources` (connectors already imports sources; avoid the reverse import).

Accepted shapes (from spec table): `news_feed {news, event}`, `text_panel {news}`, `table_panel {news, measurement, event}`, `finance_chart {measurement}`, `provider_observation {measurement}`, `weather {weather}`, `map {located}`, `intelligence_panel {located}`, `telegram_feed {messages}`, `video_panel {video}`, `github_project {project_activity}`. `highlights`, `watch_rules`: `None` (unchanged). A source is compatible when its shapes intersect `accepted_shapes`.

- [ ] **Step 1: Write failing tests** in `test_gadget_shapes.py`:

```python
def test_shape_rules() -> None:
    gadgets.validate_renderer_sources("finance_chart", [("noaa_kp", "api")])
    gadgets.validate_renderer_sources("map", [("usgs", "api")])
    gadgets.validate_renderer_sources("provider_observation", [("usgs", "api")])  # legacy USGS Metric stays valid
    for renderer, source in [("telegram_feed", ("bbc_world", "rss")), ("video_panel", ("usgs", "api")),
                             ("map", ("cisa_kev", "api")), ("weather", ("noaa_kp", "api"))]:
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_renderer_sources(renderer, [source])
        assert (exc.value.code, exc.value.field) == ("source_incompatible", "source_ids")

def test_renderer_read_exposes_shapes() -> None:
    reads = {r.id: r for r in public.renderer_reads()}
    assert reads["finance_chart"].accepted_shapes == ["measurement"]
    assert reads["highlights"].accepted_shapes is None

async def test_compatible_counts_not_capped_by_page(...) -> None:
    # 150 active noaa_kp sources + 1 paused + 1 bbc_world → finance_chart == 150, news_feed == 1
```
Rewrite `test_catalog_read` / `test_template_providers_subset_of_renderer_and_known` in `test_gadgets_templates.py` against `accepted_shapes` (they currently reference a `fear_greed` template that no longer exists).

- [ ] **Step 2: Run** `pytest tests/unit/modules/dashboard/test_gadget_shapes.py tests/unit/modules/dashboard/test_gadgets_templates.py -q` → FAIL.
- [ ] **Step 3: Implement** `GadgetConfigError(ValueError)` with `code: str` and `field: str | None` in `gadgets.py`; in `routes.py::_call` put `{"field": exc.field}` into `details` when present (code already read via `getattr`). Switch `_validate_selected_sources` to pass `(row.provider, row.type)`. Add the two routes and `last_success_at`.
- [ ] **Step 4: Run** the dashboard and sources unit suites: `pytest tests/unit/modules/dashboard tests/unit/modules/sources -q` → PASS.
- [ ] **Step 5: Commit** `feat(dashboard): shape-based renderer compatibility and counts`

---

### Task 4: Measurement scope validation (backend)

**Files:**
- Modify: `modules/dashboard/gadgets.py:147-184` (`validate_renderer_configuration`), add `validate_measurement_scope`
- Modify: `modules/dashboard/public.py` (call it from create/patch after sources are loaded)
- Test: `tests/unit/modules/dashboard/test_gadget_shapes.py`

**Interfaces:**
- Consumes: `provider_metrics` (Task 2), `GadgetConfigError` (Task 3).
- Produces: `validate_measurement_scope(renderer_id: str, provider: str | None, scope: GadgetScope) -> None` for `finance_chart`, `provider_observation`, `weather`.

Rules:
- `finance_chart`: exactly one metric, in the provider's catalog → else `metric_not_in_catalog` / field `metrics`. Symbol required iff `symbol_keyed` → else `symbol_required` / field `symbols`.
- `provider_observation`: metrics may be empty (all); any given metric must be in the catalog.
- `weather`: metrics in `open_meteo` catalog (replaces the inline Open-Meteo allowlist).
- Remove the hard-coded OHLCV and Open-Meteo checks from `validate_renderer_configuration`; keep its scope-field allowlist and map/intelligence checks. Map layer outside catalog stays an error with code `map_layer_unavailable`, field `map_layer_ids`.
- On PATCH, run measurement validation only when `scope` or `source_ids` changed, so legacy rows save on rename.

- [ ] **Step 1: Write failing tests**

```python
def test_chart_accepts_symbolless_measurement() -> None:
    gadgets.validate_measurement_scope("finance_chart", "noaa_kp", GadgetScope(metrics=["kp_index"]))

def test_chart_errors_are_field_tagged() -> None:
    cases = [("alpha_vantage", GadgetScope(metrics=["close"]), ("symbol_required", "symbols")),
             ("noaa_kp", GadgetScope(metrics=["close"]), ("metric_not_in_catalog", "metrics"))]
    for provider, scope, expected in cases:
        with pytest.raises(gadgets.GadgetConfigError) as exc:
            gadgets.validate_measurement_scope("finance_chart", provider, scope)
        assert (exc.value.code, exc.value.field) == expected

def test_legacy_alpha_vantage_chart_still_valid() -> None:
    gadgets.validate_measurement_scope("finance_chart", "alpha_vantage", GadgetScope(symbols=["NVDA"], metrics=["close"]))

async def test_patch_name_only_keeps_incompatible_source(...) -> None:
    # saved news_feed bound to a telegram source; PATCH {name} only → 200, source_ids unchanged

async def test_create_returns_field_in_422_details(client) -> None:
    # POST finance_chart with alpha_vantage source and no symbol → 422, detail.code == "symbol_required", detail.details.field == "symbols"
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/unit/modules/dashboard -q` → PASS; `ruff check modules/dashboard modules/connectors modules/sources` and `mypy modules/dashboard modules/connectors modules/sources` clean.
- [ ] **Step 5: Commit** `feat(dashboard): validate chart and metric scope against provider catalog`

---

### Task 5: Web API types and Chart adapter

**Files:**
- Modify: `apps/web/src/modules/dashboard/api.ts:128-163` (types), add fetchers + `dashboardKeys.compatibleCounts`, `dashboardKeys.metricCatalog`
- Modify: `apps/web/src/modules/dashboard/gadgets/finance-chart.tsx`
- Modify: `apps/web/src/modules/sources/api.ts` (`ConnectorCatalogEntry.data_shapes`, `BuiltinSource.data_shapes`)
- Test: `apps/web/src/modules/dashboard/gadgets/finance-chart.test.tsx` (new)

**Interfaces:**
- Produces (TS):
  ```ts
  export type DataShape = 'news' | 'measurement' | 'event' | 'located' | 'messages' | 'video' | 'project_activity' | 'weather';
  // GadgetRenderer: replace source_providers with accepted_shapes: DataShape[] | null
  // GadgetTemplate: drop providers, add accepted_shapes: DataShape[] | null
  // GadgetSource: add last_success_at: string | null, data_shapes: DataShape[]
  export type ProviderMetrics = { metrics: { id: string; unit: string | null }[]; symbol_keyed: boolean; suggested_symbols: string[] };
  export function listCompatibleSourceCounts(signal?: AbortSignal): Promise<Record<string, number>>;
  export function getMetricCatalog(signal?: AbortSignal): Promise<Record<string, ProviderMetrics>>;
  ```
- `FinanceChart` needs the bound source's provider: read it from the existing gadget instance/source projection if present; otherwise fetch the source row via `listGadgetSources`. Symbol filter applies only when the provider entry is `symbol_keyed`; "symbol required" status shows only then.

- [ ] **Step 1: Write failing tests**: `renders a symbol-less kp_index series` (catalog `noaa_kp` not symbol_keyed, observations without symbol → chart rendered, no `financeSourceSymbolRequired` text) and `still requires a symbol for alpha_vantage`.
- [ ] **Step 2: Run** `npx vitest run src/modules/dashboard/gadgets/finance-chart.test.tsx` → FAIL.
- [ ] **Step 3: Implement.** Update every TS usage of `source_providers` / `template.providers` (`gadget-editor.tsx`, tests) so `npx tsc --noEmit` passes.
- [ ] **Step 4: Run** the test, then `npx tsc --noEmit` → PASS.
- [ ] **Step 5: Commit** `feat(web): chart reads any measurement series`

---

### Task 6: Editor steps 1–2 (tiles, shape-filtered picker)

**Files:**
- Create: `apps/web/src/modules/dashboard/sources-link.ts`
- Modify: `apps/web/src/modules/dashboard/gadget-editor.tsx` (`GadgetEditor` template grid, `DatasourcePicker`)
- Modify: `apps/web/src/core/messages/gadget-settings.ts`
- Test: `apps/web/src/modules/dashboard/gadget-editor.test.tsx`

**Interfaces:**
- Consumes: Task 5 types/fetchers; `GadgetSource.data_shapes` and `last_success_at` from Task 3's `GadgetSourceOptionPage` (add `data_shapes: DataShape[]` to the TS `GadgetSource` type in Task 5).
- Produces: `sourcesLinkFor(renderer: GadgetRenderer): string` → `/settings/sources?shapes=<comma list>&for=<renderer id>`; returns `/settings/sources` when `accepted_shapes` is null.
- `DatasourcePicker` props: replace `providers` with `acceptedShapes: DataShape[] | null`.

Behaviour:
- Each tile shows count text (`compatibleCount`, plural ICU) for renderers with `source_mode !== 'none'`. Count 0 → tile renders as a link (`<Link href={sourcesLinkFor(renderer)}>`) with "Add a compatible datasource" copy, not a button that opens the editor.
- Picker lists active sources whose `data_shapes` intersect `acceptedShapes`; each row shows name, provider label (`sources.providers.<id>` if present, else id) and `last_success_at` formatted with `formatDateTime` (or "never updated").
- Picker follows `next_cursor` (load-more button) instead of stopping at 100.

- [ ] **Step 1: Write failing tests**: `shows compatible counts on tiles`, `zero-count tile links to filtered sources` (href `/settings/sources?shapes=measurement&for=finance_chart`), `picker hides incompatible sources` (bbc_world absent for finance_chart, noaa_kp present), `shows provider and last update`, `loads next source page`.
- [ ] **Step 2: Run** `npx vitest run src/modules/dashboard/gadget-editor.test.tsx` → FAIL.
- [ ] **Step 3: Implement**, vi-vi + en-us copy.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(web): display-first tiles with compatible source counts`

---

### Task 7: Editor step 3 (single name, relevant fields, pickers, field errors)

**Files:**
- Modify: `apps/web/src/modules/dashboard/gadget-editor.tsx`, `apps/web/src/modules/dashboard/layout-editor.tsx:146,181,325-337`
- Modify: `apps/web/src/core/messages/gadget-settings.ts` (remove `templates.*` in both locales; add field-error and metric-name copy), `apps/web/src/core/messages/dashboard.ts`
- Test: `apps/web/src/modules/dashboard/gadget-editor.test.tsx`, `apps/web/src/modules/dashboard/layout-editor.test.tsx`

**Interfaces:**
- Consumes: `getMetricCatalog` (Task 5), 422 `details.field` / `code` from Task 4 via `ApiError.details`.
- Produces: `fieldsFor(renderer: string): ReadonlySet<'keywords' | 'limit' | 'symbols' | 'metrics' | 'lookback' | 'map' | 'cii' | 'channels' | 'rules'>` (exported for tests).

Field relevance: `keywords`/`limit`/`rules` only for `news_feed, text_panel, table_panel, video_panel, telegram_feed, highlights, watch_rules`; `metrics`+`lookback` for `finance_chart, provider_observation, weather`; `symbols` for `finance_chart`/`provider_observation` only when the selected source's provider is `symbol_keyed`; `map` for `map`; `cii` for `intelligence_panel`; `channels` for `telegram_feed`.

Behaviour:
- Metric = shadcn `Select` (single for chart, checkbox list for metric/weather) from the selected source's catalog entry, labels from `gadgetSettings.metricNames.<id>` falling back to id, unit shown. Symbol = `SearchableSelect` (`apps/web/src/components/ui/searchable-select.tsx`) over `suggested_symbols` that also accepts typed text, uppercased.
- Chart default metric = first catalog metric when the source changes.
- Unavailable map layers (`layerId !== 'world_observations'`) render disabled checkboxes.
- On save error with `details.field`, show the localized `fieldErrors.<code>` under that field and open Advanced if the field is inside it; fallback remains `saveFailed`.
- `choose(next)` derives defaults from `templates.data.find(t => t.renderer === next)`, not the stale `template`; drop the duplicate `setLookback`.
- `layout-editor.tsx`: remove the instance-title input and `instanceTitle` state; `onAddGadgetInstance` gets `title: undefined` so the definition name is the title.

- [ ] **Step 1: Write failing tests**: `hides keywords and limit for weather`, `chart metric picker lists noaa_kp metrics`, `symbol field only for symbol-keyed provider`, `maps 422 symbol_required to the symbol field`, `choose applies defaults of the chosen template` (two templates with different `filters.limit`, pick B after A → limit is B's), `unavailable map layers disabled`; in `layout-editor.test.tsx`: `add dialog has one name field`.
- [ ] **Step 2: Run** both test files → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `npx tsc --noEmit`; `npx eslint src/modules/dashboard src/core/messages`.
- [ ] **Step 5: Commit** `feat(web): renderer-relevant gadget fields with catalog pickers`

---

### Task 8: Settings → Sources shape filter

**Files:**
- Modify: `apps/web/src/modules/sources/source-list.tsx`, `builtin-sources.tsx` (`BuiltinWizard`), `connector-editor.tsx:462-475` (catalog grid)
- Modify: `apps/web/src/core/messages/sources.ts`
- Test: `apps/web/src/modules/sources/source-list-filter.test.tsx` (new)

**Interfaces:**
- Consumes: `?shapes=` and `?for=` (Task 6 `sourcesLinkFor`), `data_shapes` on `BuiltinSource` / `ConnectorCatalogEntry` (Tasks 1, 5).
- Produces: `parseShapeFilter(params: URLSearchParams): { shapes: DataShape[]; renderer: string | null }` — unknown shape strings dropped; empty result = no filter.

Behaviour:
- `SourceList` reads `useSearchParams()`. With a filter it shows a banner: "Showing datasources for: <localized renderer name>" (reuse `rendererName` from `gadget-editor.tsx`), plus "Add built-in", "Add connector" and "Clear filter" (removes both params via `router.replace`).
- `BuiltinWizard` and the connector add grid accept `shapeFilter?: DataShape[]` and keep only entries whose `data_shapes` intersect it; if the filter leaves nothing, show "No compatible datasource can be added" with the clear action.
- Existing source rows are not filtered.

- [ ] **Step 1: Write failing tests**: `shows banner for ?shapes=measurement&for=finance_chart`, `builtin wizard lists only measurement built-ins` (noaa_kp shown, bbc_world hidden), `ignores unknown shapes`, `clear filter removes params`.
- [ ] **Step 2: Run** `npx vitest run src/modules/sources/source-list-filter.test.tsx` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** this test plus `builtin-sources.test.tsx source-wizard.test.tsx source-list-mcp.test.tsx` → PASS.
- [ ] **Step 5: Commit** `feat(web): filter Sources add flows by gadget data shape`

---

### Task 9: Presets

**Files:**
- Modify: `modules/dashboard/gadgets.py:71-105,187-189` (`PRESETS`, `dashboard_preset`)
- Modify: `apps/web/src/modules/dashboard/preset-picker.tsx`
- Test: `tests/unit/modules/dashboard/test_gadget_shapes.py`, `apps/web/src/modules/dashboard/preset-picker.test.tsx` (new)

**Interfaces:**
- Consumes: `sourcesLinkFor` (Task 6), `rendererName`/`templateName` from `gadget-editor.tsx` (export `templateName`).
- Produces: `PRESET_ALIASES: Mapping[str, str] = {"tech": "technology", "finance_variant": "finance"}`; `dashboard_preset(preset_id)` resolves aliases; `list_presets` returns only canonical ids.

Backend:
- Remove presets `tech`, `finance_variant`; remove slots whose renderer is `planned` (`research`, `personal_context`) from every preset. Before deleting ids, re-run `grep -rn "preset_id" modules tests` and confirm no persisted column stores preset ids (builder check found only uuid5 keys and preview payloads).

Frontend:
- Preset list subtitle and slot rows use `templateName(slot.renderer, t)`, not raw ids; family badge localized or removed.
- Remove fingerprint line and the hard-coded `20 cols` / `stack` line.
- "Needs setup" link: `sourcesLinkFor(renderer)` for that slot's renderer.
- Cancel: `<Button variant="outline">` (no `className="secondary"`).
- Count text "({n} gadgets)" moves to an ICU message in both locales.

- [ ] **Step 1: Write failing tests**: `test_removed_preset_ids_alias` (`dashboard_preset("tech").id == "technology"`), `test_presets_have_no_planned_slots`, `test_preset_ids_unique_labels` (no two presets share a label); web: `slots show localized names`, `no fingerprint text`, `needs-setup link is shape filtered`.
- [ ] **Step 2: Run** backend + web tests → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/unit/modules/dashboard -q` and `npx vitest run src/modules/dashboard` → PASS.
- [ ] **Step 5: Commit** `feat(dashboard): clean preset catalog and picker`

---

### Task 10: Final verification

- [ ] **Step 1:** `.venv/Scripts/python.exe -m pytest tests/unit/modules/dashboard tests/unit/modules/connectors tests/unit/modules/sources -q` → all pass.
- [ ] **Step 2:** From `apps/web`: `npx vitest run src/modules/dashboard src/modules/sources`, `npx tsc --noEmit`, `npx eslint src/modules/dashboard src/modules/sources src/core/messages` → clean.
- [ ] **Step 3:** `ruff check modules` and `mypy modules/dashboard modules/connectors modules/sources` → clean.
- [ ] **Step 4:** `gitnexus_detect_changes({scope: "all"})` → only the files in File Structure changed.
- [ ] **Step 5:** Ask the owner whether to run the Docker dashboard/datasource e2e group; report results as run.
