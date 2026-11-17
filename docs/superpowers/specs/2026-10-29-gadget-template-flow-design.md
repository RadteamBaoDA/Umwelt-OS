# Gadget template flow: display-first, data-shape compatible

Date: 2026-10-29 · Branch context: `feature/fix-ui` · Status: draft for owner approval

## Intent

Adding a gadget on the Dashboard should never lead the owner into a dead end. Today the flow is structurally
correct (gadgets only bind already-configured active sources; the backend re-validates providers; failed
placement deletes the new definition) but it is not friendly:

- Every display template is shown even when no compatible source exists; the owner only discovers this after
  picking it. Weather (`open_meteo`) and Chart (`alpha_vantage`) cannot be satisfied by any built-in source.
- Compatibility is a per-renderer provider allowlist, or "any provider" for 9 renderers. So an RSS feed can be
  bound to the Telegram message list or the video panel, while 17 keyless measurement/event built-ins
  (`emsc_quakes`, `bmkg_quakes`, `noaa_kp`, `iss_position`, `oecd_cli`, `nasa_eonet`, …) fit no
  numeric/geo gadget at all.
- Configuration asks for technical codes (`temperature_2m`, numeric Telegram channel IDs, CII codes) and
  reports backend allowlist violations as a generic "Save failed".
- The preset picker shows raw renderer IDs, a preview fingerprint and a hard-coded "20 cols"; it has duplicate
  presets and slots for `planned` renderers that can never become ready.

The owner thinks **display type first**, then picks among sources they already added.

## Goals

1. **Step 1 – pick a display type.** Keep the template grid (one template per available renderer). Each tile
   shows how many compatible active sources the workspace has. A tile with zero compatible sources stays
   visible and offers a link to Settings → Sources filtered to compatible providers (instead of opening the
   editor into an empty picker).
2. **Step 2 – pick a compatible source.** The datasource picker lists only active sources whose **data shape**
   the renderer can display. Each row shows name, provider label and last successful update time.
3. **Compatibility by data shape.** Each provider/source type declares the shape of data it produces; each
   renderer declares the shapes it accepts. The same rule drives the dashboard picker, the tile counts, the
   Sources filter, the preset readiness, and backend validation on save. Target mapping:

   | Data shape | Renderers |
   |---|---|
   | news / documents | Feed (`news_feed`), Text (`text_panel`), Table (`table_panel`) |
   | measurement (numeric value + time) | Chart (`finance_chart`), Metric (`provider_observation`), Weather (`weather`, Open-Meteo shape only), Table |
   | event with location | Map (`map`), Related signals (`intelligence_panel`), Feed, Table |
   | messages | Message list (`telegram_feed`) |
   | video | Video (`video_panel`) |
   | project activity | Activity (`github_project`) |

   `highlights`, `watch_rules`, `tasks`, `goals` and `daily_brief` keep their current source behaviour; the
   builder confirms from each renderer's data adapter whether they read sources and of which shape.

   - **Location is a separate attribute, not implied by `event`.** Add a per-provider "has coordinates" flag
     (and the equivalent for non-catalog source types). Only located sources are compatible with Map and
     Related signals, so `cisa_kev`, `nvd`, `launch_library`, `ioda`, `us_travel_advisories` etc. never produce
     an empty map. A source may carry several shapes: `usgs` is both measurement and located event, so existing
     USGS Metric gadgets stay compatible.
   - **Table and Feed with measurement/event sources** keep showing the existing document rows
     (`listGadgetDocumentProjections`). No numeric value/time table columns in this work.
3a. **Chart widens to every measurement source.** Owner decision: picking `noaa_kp`, `binance`, `oecd_cli`,
   `frankfurter`, `world_bank`, etc. must draw a line chart, not only a single number.
   - Add a backend **per-provider metric catalog** for measurement providers: metric id, localized label, unit,
     and whether the provider is keyed by a symbol/series (e.g. currency pair, country, coin) and the allowed
     or suggested symbols. Publish it through a dashboard-readable public contract (template/renderer read or a
     sources/connectors `public.py` query); the dashboard must not import connector internals.
   - Generalize the Chart adapter and `validate_renderer_configuration`: symbol is required only for providers
     whose catalog says they are symbol-keyed; metric must be in that provider's catalog entry. The existing
     Alpha Vantage OHLCV path (symbol + open/high/low/close/volume) keeps working unchanged for saved gadgets.
   - Chart reads measurement series through the same observation window contract the Metric gadget already
     uses (`listWorldObservationWindow` / `modules/knowledge/observations`), unless that contract cannot
     return a bounded per-metric time series, in which case extend it there.
   - The Metric gadget's metric picker uses the same catalog (it has no allowlist today).
   - Every measurement provider in `BUILTIN_PROVIDERS` must have a catalog entry; add an import-time or unit
     check that fails when one is missing, so a compatible-looking source can never fail on save.
4. **Settings → Sources filter.** The link carries the display type (e.g. `/settings/sources?display=chart`).
   The Sources page then shows only built-ins and addable providers compatible with that display type, with a
   visible "filtered for: Chart" chip and a way to clear the filter. The owner returns to the dashboard on
   their own; no auto-return or pre-selection.
5. **Step 3 – configure.**
   - One name field. The add dialog's separate "instance title" field goes away; the definition name is the
     gadget title.
   - Only fields meaningful for the chosen renderer are shown (no keywords/limit for Tasks, Goals, Weather,
     Metric, Chart, Activity).
   - Metrics, symbols, Telegram channels and map layers are chosen from lists, not typed. Metric and symbol
     lists come from the per-provider metric catalog (goal 3a) for the selected source; Weather keeps its
     Open-Meteo allowlist. Channel lists come
     from the selected Telegram source if that data is available to the dashboard through a public contract;
     otherwise keep free text with format help.
   - Unavailable map layers are disabled, not just labelled.
   - Backend validation errors map to the offending field with a localized message, not "Save failed".
   - Fix `choose()` in `apps/web/src/modules/dashboard/gadget-editor.tsx`: it reads `template` from the
     previous renderer state, so defaults would come from the wrong template once templates carry defaults.
6. **Preset picker** (`apps/web/src/modules/dashboard/preset-picker.tsx`, `modules/dashboard/gadgets.py`):
   - Show localized display-type names per slot; remove fingerprint text and the hard-coded layout line.
   - Merge duplicates: `technology` + `tech`, `finance` + `finance_variant` (keep one id each; see constraints).
   - Remove slots whose renderer is `planned` (`research`, `personal_context`). Presets left with one slot stay.
   - "Needs setup" links use the same filtered Sources link for the slot's display type.
   - Fix the Cancel button (`className="secondary"` → `variant="secondary"` / outline per design system).
7. Remove dead i18n copy `gadgetSettings.templates.*` (provider-specific template names no longer used).

## Non-goals

- Data-first flow ("I want BTC price → suggest a gadget").
- Auto-return from Sources to the add dialog or auto-selecting a newly added source.
- Creating or activating sources from the dashboard. Source creation stays in Settings.
- New renderers or new providers.
- Discovering metrics from live source data (the metric catalog is static, maintained with the provider).
- Numeric value/time columns in the Table gadget.

## Anti-goals (failure even if it "works")

- A tile, link or preset slot that leads to a screen where the owner cannot complete the task.
- Frontend-only compatibility: the backend must enforce the same rule on create/patch.
- Breaking existing saved gadgets: a saved definition whose source is now considered incompatible must still
  render and still allow unrelated edits (current behaviour sends `source_ids` only when changed — keep it),
  with the incompatible badge shown.
- Leaking connector configuration or credentials into the dashboard source-selection projection.

## Constraints

- Modular monolith: dashboard reads sources only via `modules/sources/public.py` contracts. If source rows
  need a data-shape or last-update field, extend `GadgetSourceSelection` there; do not import connector
  internals from dashboard.
- Reuse `FreeProviderSpec.data_kind` (`news | measurement | event`) in `modules/connectors/provider_specs.py`
  as the starting point for provider shape. Non-catalog source types (Telegram, YouTube/video, GitHub, custom
  RSS, MCP, browser) need a shape too; derive it where the source type is owned.
- Preset IDs are persisted references (applied presets, possibly dashboards). Merging duplicates must keep
  old IDs resolvable or migrate them; never break an existing dashboard.
- UI follows `docs/DESIGN_SYSTEM.md` and the `umwelt-os-ui-system` skill: shadcn primitives, semantic tokens,
  en-us / vi-vi copy for every new string.
- Mandatory code documentation (JSDoc / docstrings) per `AGENTS.md`; GitNexus impact analysis before editing
  symbols.
- Tests: Vitest for editor/picker/Sources filter behaviour, pytest for the compatibility rule and preset
  changes. Docker e2e only with owner approval.

## Decided by the owner

- Display type first, then filter already-added compatible sources.
- Zero compatible sources → hyperlink to Settings → Sources, filtered to compatible providers.
- No auto-return to the dashboard after adding a source.
- Preset picker fixes are in this same piece of work.
- Chart is widened to every measurement source in this same piece of work (option "a"), including the
  per-provider metric catalog, rather than staying Alpha Vantage only.

## Calls made by Claude (owner may override)

- Compatibility is by data shape, declared by provider/source type, rather than per-renderer provider lists.
- Tiles with zero sources stay visible (with the link) instead of being hidden.
- One name field; the instance-title field is removed.
- Metric choices come from a static per-provider catalog, not live data.
- Location is a separate per-provider flag; a source may have several shapes (USGS stays Metric-compatible).
- Table/Feed keep document rows for measurement/event sources.
- Duplicate presets merged; planned-renderer slots removed. (Builder check found preset IDs used only for
  uuid5 definition keys and preview payloads, no persisted column; builder re-verifies before merging.)

## Left to the builder

- Exact shape enum names and where each non-catalog source type declares its shape.
- How the frontend gets compatible-source counts (extend renderer/template read vs. client-side join with the
  source-selection page). The source-selection page is capped at 100 per page; counts must not silently
  undercount when a workspace has more.
- Query-param name and how the Sources page filters built-ins vs. the add-source wizard.
- Whether Weather is a shape (`measurement` + weather metrics) or stays pinned to `open_meteo`.
- Error-code → field mapping for backend validation messages.
