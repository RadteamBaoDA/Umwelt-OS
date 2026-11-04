import type {
  GadgetDefinition, GadgetFilters, GadgetScope, GadgetTemplate, HighlightRule, ProviderMetrics,
} from './api';
import { CHART_FALLBACK_LOOKBACK_DAYS, METRIC_FALLBACK_LOOKBACK_DAYS } from './gadgets/use-effective-lookback';

/** Hard ceiling of selectable datasources in multi mode. */
export const MAX_SOURCES = 32;
/** Live-map providers the owner may toggle. */
export const LIVE_PROVIDERS = ['usgs', 'eonet', 'firms', 'hackernews', 'lobsters', 'cisa_kev', 'nvd', 'business', 'fed', 'github_releases', 'sec'] as const;
/** Live-map providers enabled for a new map gadget. */
export const DEFAULT_LIVE_PROVIDERS = ['usgs', 'eonet', 'hackernews', 'lobsters', 'cisa_kev', 'nvd', 'business', 'fed'];
/** Map layers offered by the selected-sources map. */
export const MAP_LAYERS = ['world_observations', 'military', 'economic', 'disaster', 'escalation'] as const;

/** Editor field groups a renderer may expose. */
export type FieldKey = 'keywords' | 'limit' | 'symbols' | 'metrics' | 'lookback' | 'map' | 'cii' | 'channels' | 'rules';
const LIST_RENDERERS = ['news_feed', 'text_panel', 'table_panel', 'job_list', 'video_panel', 'telegram_feed', 'highlights', 'watch_rules'];

/** Weather overlay choices of the live map. */
export type WeatherLayer = 'none' | 'temperature' | 'precipitation' | 'wind' | 'clouds';

/**
 * Editor fields that matter to one renderer. `symbols` is only a candidate here: the editor additionally requires the
 * selected datasource's provider to be symbol-keyed before showing it.
 *
 * @param renderer Renderer id.
 * @returns The set of field keys the renderer reads.
 */
export function fieldsFor(renderer: string): ReadonlySet<FieldKey> {
  const out = new Set<FieldKey>();
  if (LIST_RENDERERS.includes(renderer)) (['keywords', 'limit', 'rules'] as const).forEach((key) => out.add(key));
  if (['finance_chart', 'provider_observation', 'weather'].includes(renderer)) { out.add('metrics'); out.add('lookback'); }
  if (renderer === 'finance_chart' || renderer === 'provider_observation') out.add('symbols');
  if (renderer === 'map') out.add('map');
  if (renderer === 'intelligence_panel') out.add('cii');
  if (renderer === 'telegram_feed') out.add('channels');
  return out;
}

/**
 * Default chart metric for a provider's catalog entry: `close` when listed, else the first metric
 * (the backend `default_metric` applies the same rule). Callers pass an entry; with none (or an entry listing no
 * metrics) the `close` fallback is only a display/typing hint and must never be persisted for an unknown provider.
 *
 * @param entry Catalog entry of the bound provider, if known.
 * @returns A metric id.
 */
export function defaultMetric(entry?: ProviderMetrics): string {
  const ids = entry?.metrics.map((item) => item.id) ?? [];
  return ids.includes('close') || ids.length === 0 ? 'close' : ids[0];
}

/**
 * Window (days) a new gadget of `renderer` starts with when the provider catalog has no default: the value the renderer
 * itself falls back to at runtime (Metric 30, chart 90) so an untouched editor and an unset stored gadget agree.
 */
export function fallbackLookback(renderer: string): number {
  if (renderer === 'map' || renderer === 'intelligence_panel' || renderer === 'provider_observation') return METRIC_FALLBACK_LOOKBACK_DAYS;
  return CHART_FALLBACK_LOOKBACK_DAYS;
}

/** True when a code-less FastAPI/pydantic 422 `details` list names `lookback_days` in an item's `loc` path. */
export function namesLookbackField(details: unknown): boolean {
  return Array.isArray(details) && details.some((item) => Array.isArray((item as { loc?: unknown })?.loc) && ((item as { loc: unknown[] }).loc).includes('lookback_days'));
}

/**
 * Merges the catalog entries of several bound providers into one pseudo-entry that drives the editor's pickers.
 * Metrics and suggestions are unioned (a metric's unit becomes null when providers disagree), "keyed" and open-catalog
 * flags are true when any provider has them, `seed_symbol`/`charts_as_series` only when all do, the lookback is the
 * widest default. One entry is returned as is; an empty list or any unknown entry gives undefined so the free-text
 * fallbacks stay in place. Pure.
 *
 * @param entries Catalog entry per distinct bound provider (undefined when its catalog entry is unknown).
 * @returns The merged entry, or undefined when nothing is known for every provider.
 */
export function mergeCatalogEntries(entries: readonly (ProviderMetrics | undefined)[]): ProviderMetrics | undefined {
  if (entries.length === 0 || entries.some((item) => item === undefined)) return undefined;
  const known = entries as ProviderMetrics[];
  if (known.length === 1) return known[0];
  const metrics = new Map<string, string | null>();
  for (const item of known) for (const metric of item.metrics) metrics.set(metric.id, metrics.has(metric.id) && metrics.get(metric.id) !== metric.unit ? null : metric.unit);
  const lookbacks = known.flatMap((item) => (item.default_lookback_days === null ? [] : [item.default_lookback_days]));
  const cadences = known.map((item) => item.cadence_days);
  return {
    metrics: [...metrics].map(([id, unit]) => ({ id, unit })),
    symbol_keyed: known.some((item) => item.symbol_keyed),
    suggested_symbols: [...new Set(known.flatMap((item) => item.suggested_symbols))],
    metrics_open: known.some((item) => item.metrics_open),
    charts_as_series: known.every((item) => item.charts_as_series),
    region_keyed: known.some((item) => item.region_keyed),
    suggested_regions: [...new Set(known.flatMap((item) => item.suggested_regions))],
    seed_symbol: known.every((item) => item.seed_symbol),
    cadence_days: cadences.some((item) => item === null) ? null : Math.min(...(cadences as number[])),
    default_lookback_days: lookbacks.length > 0 ? Math.max(...lookbacks) : null,
  };
}

/** Splits comma-separated text into trimmed, non-empty entries (optionally upper-cased). */
export function splitList(value: string, upper = false): string[] {
  return value.split(',').map((item) => (upper ? item.trim().toUpperCase() : item.trim())).filter(Boolean);
}

/** Every editable field of the gadget editor (one object instead of 26 separate states). */
export interface GadgetDraft {
  /** Selected template renderer; null shows the template chooser. */
  choice: string | null;
  renderer: string;
  name: string;
  sourceIds: string[];
  keywords: string;
  exclude: string;
  limit: string;
  symbols: string;
  metrics: string;
  lookback: string;
  /** Once the owner typed a lookback, a later provider change must not overwrite it with the catalog default. */
  lookbackTouched: boolean;
  regions: string;
  /** Chart region (one value: names such as "Korea, Rep." contain commas, so it is not a comma list). */
  region: string;
  channels: string;
  cii: string;
  layers: string[];
  mapMode: 'live' | 'selected_sources';
  liveProviders: string[];
  country: string;
  githubOwner: string;
  githubRepo: string;
  secCik: string;
  weatherLayer: WeatherLayer;
  engine: 'globe' | 'flat';
  precise: boolean;
  rules: HighlightRule[];
  rulesTouched: boolean;
  touched: boolean;
  /** Latest saved revision so a retry after a failed follow-up step does not conflict. */
  base: GadgetDefinition | null;
}

/** Initial live-map provider list for an edited definition (saved list, or defaults for non-live maps). */
function initialLiveProviders(definition: GadgetDefinition | null): string[] {
  if (definition?.filters?.map_mode === 'live') return definition.scope?.live_map_providers ?? [];
  return definition?.scope?.live_map_providers?.length ? definition.scope.live_map_providers : DEFAULT_LIVE_PROVIDERS;
}

/** Joins an optional list into the comma-separated text the editor fields hold. */
const joined = (list: readonly string[] | undefined): string => (list ?? []).join(', ');

/** Filter-derived text fields of a draft (keyword lists and item limit). */
function filterFieldsOf(filters: GadgetFilters | undefined): Pick<GadgetDraft, 'keywords' | 'exclude' | 'limit'> {
  return { keywords: joined(filters?.keywords), exclude: joined(filters?.exclude_keywords), limit: String(filters?.limit ?? 25) };
}

/** Scope-derived text fields of a draft (symbols, metrics, regions, channels, countries, layers). */
function scopeFieldsOf(scope: GadgetScope | undefined): Pick<GadgetDraft, 'symbols' | 'metrics' | 'regions' | 'region' | 'channels' | 'cii' | 'layers'> {
  return {
    symbols: joined(scope?.symbols),
    metrics: joined(scope?.metrics),
    regions: joined(scope?.regions),
    region: scope?.regions?.[0] ?? '',
    channels: joined(scope?.channel_ids),
    cii: joined(scope?.cii_country_codes),
    layers: scope?.map_layer_ids ?? [],
  };
}

/** Map-mode fields of a draft that come from a definition's filters. */
function mapFilterFieldsOf(definition: GadgetDefinition | null): Pick<GadgetDraft, 'mapMode' | 'weatherLayer' | 'engine' | 'precise'> {
  const filters = definition?.filters;
  return {
    mapMode: definition ? filters?.map_mode ?? 'selected_sources' : 'live',
    weatherLayer: filters?.map_weather_layer ?? 'none',
    engine: filters?.map_engine ?? 'globe',
    precise: filters?.show_precise_locations ?? false,
  };
}

/** Live-map fields of a draft that come from a definition's scope. */
function liveMapFieldsOf(definition: GadgetDefinition | null): Pick<GadgetDraft, 'liveProviders' | 'country' | 'githubOwner' | 'githubRepo' | 'secCik'> {
  const scope = definition?.scope;
  return {
    liveProviders: initialLiveProviders(definition),
    country: scope?.live_map_country ?? '',
    githubOwner: scope?.live_map_github_owner ?? '',
    githubRepo: scope?.live_map_github_repo ?? '',
    secCik: scope?.live_map_sec_cik ?? '',
  };
}

/**
 * Builds the starting draft for a saved definition, or the blank draft of a new gadget.
 * A saved definition with no lookback keeps it unset (empty field) so a rename never invents one.
 */
export function initialDraft(definition: GadgetDefinition | null): GadgetDraft {
  const renderer = definition?.renderer ?? '';
  return {
    choice: definition ? definition.renderer : null,
    renderer,
    name: definition?.name ?? '',
    sourceIds: definition?.source_ids ?? [],
    ...filterFieldsOf(definition?.filters),
    ...scopeFieldsOf(definition?.scope),
    lookback: definition ? String(definition.scope?.lookback_days ?? '') : String(fallbackLookback(renderer)),
    lookbackTouched: false,
    ...mapFilterFieldsOf(definition),
    ...liveMapFieldsOf(definition),
    rules: definition?.highlight_rules ?? [],
    rulesTouched: false,
    touched: false,
    base: definition,
  };
}

/**
 * Draft patch that starts a new definition from one available renderer and clears configuration from any prior
 * selection, so nothing typed for a previous template leaks into the next one.
 *
 * @param picked The template of the chosen renderer (undefined when the catalog has none).
 * @param renderer The chosen renderer id.
 * @param name Localized default gadget name.
 */
export function draftPatchForTemplate(picked: GadgetTemplate | undefined, renderer: string, name: string): Partial<GadgetDraft> {
  return {
    choice: renderer,
    mapMode: 'live',
    liveProviders: DEFAULT_LIVE_PROVIDERS,
    country: '', githubOwner: '', githubRepo: '', secCik: '', weatherLayer: 'none',
    sourceIds: [],
    ...templateListFields(picked),
    region: '',
    lookback: String(picked?.scope.lookback_days ?? fallbackLookback(renderer)),
    lookbackTouched: false,
    limit: String(picked?.filters.limit ?? 25),
    rules: [],
    rulesTouched: false,
    renderer,
    name,
  };
}

/** Draft text fields seeded from a template's filters and scope (empty when the template has none). */
function templateListFields(picked: GadgetTemplate | undefined): Partial<GadgetDraft> {
  const filterText = filterFieldsOf(picked?.filters);
  const scopeText = scopeFieldsOf(picked?.scope);
  return {
    keywords: filterText.keywords, exclude: filterText.exclude,
    symbols: scopeText.symbols, metrics: scopeText.metrics, regions: scopeText.regions,
    channels: scopeText.channels, cii: scopeText.cii, layers: scopeText.layers,
  };
}

/** Inputs of {@link planSourceChange}. */
export interface SourceChangeContext {
  /** Newly selected datasource ids. */
  next: string[];
  draft: GadgetDraft;
  /** Provider id of a loaded source row, or null when the id is absent or its page is not loaded yet. */
  providerOf: (id: string | undefined) => string | null;
  /** Provider metric catalog (undefined while loading or failed). */
  catalog: Record<string, ProviderMetrics> | undefined;
  fields: ReadonlySet<FieldKey>;
}

/**
 * True when the current metric stays valid for the new provider entry. A changed provider must not keep a metric it
 * does not list, even when its catalog is open (typed ids are only trusted while the provider stays the same).
 */
function currentMetricFits(current: string | undefined, nextEntry: ProviderMetrics | undefined, providerChanged: boolean): boolean {
  if (current === undefined) return false;
  return Boolean(nextEntry?.metrics.some((item) => item.id === current)) || (!providerChanged && nextEntry?.metrics_open === true);
}

/**
 * Multi-provider chart metrics: one chosen metric per bound provider. Keeps metrics that some provider lists and
 * adds the default of any provider left without one.
 */
function multiProviderMetrics(ctx: SourceChangeContext, nextProviders: string[], nextEntry: ProviderMetrics): string {
  const entries = nextProviders.map((item) => ctx.catalog?.[item] as ProviderMetrics);
  const kept = splitList(ctx.draft.metrics).filter((id) => nextEntry.metrics_open || nextEntry.metrics.some((item) => item.id === id));
  const added = entries.filter((item) => item.metrics.length > 0 && !kept.some((id) => item.metrics.some((metric) => metric.id === id))).map(defaultMetric);
  return [...new Set([...kept, ...added])].join(', ');
}

/** Metric list after a datasource change (finance charts keep/ add per-provider defaults; Metric gadgets drop unlisted ids). */
function metricsAfterSourceChange(
  ctx: SourceChangeContext,
  nextProviders: string[],
  nextEntry: ProviderMetrics | undefined,
  providerChanged: boolean,
): string | undefined {
  const { draft } = ctx;
  const { renderer, metrics } = draft;
  const metricFits = currentMetricFits(splitList(metrics)[0], nextEntry, providerChanged);
  if (renderer === 'finance_chart' && nextProviders.length > 1 && nextEntry) return multiProviderMetrics(ctx, nextProviders, nextEntry);
  if (renderer === 'finance_chart' && nextEntry && nextEntry.metrics.length > 0 && !metricFits) return defaultMetric(nextEntry);
  // A Metric gadget keeps only metrics the new provider lists (the save would 422 on a stale id); typed ids of
  // an open catalog are trusted only while the provider stays the same.
  if (renderer === 'provider_observation' && providerChanged && nextEntry) {
    return splitList(metrics).filter((id) => nextEntry.metrics.some((item) => item.id === id)).join(', ');
  }
  return undefined;
}

/** Symbol text after a datasource change, or undefined when the renderer has no symbol field. */
function symbolsAfterSourceChange(
  ctx: SourceChangeContext,
  nextProviders: string[],
  nextEntry: ProviderMetrics,
  providerChanged: boolean,
): string | undefined {
  const { draft, next, providerOf } = ctx;
  if (!ctx.fields.has('symbols')) return undefined;
  let nextSymbols = splitList(draft.symbols, true);
  // Changed provider: keep only symbols it suggests; a provider without suggestions cannot validate any.
  if (providerOf(next[0]) === 'coingecko' && next[0] !== draft.sourceIds[0]) nextSymbols = [];
  // ponytail: several providers keep every typed symbol (symbols cannot be mapped to providers; the backend only requires some).
  else if (providerChanged && nextProviders.length <= 1) nextSymbols = nextSymbols.filter((symbol) => nextEntry.suggested_symbols.includes(symbol));
  if (draft.renderer === 'finance_chart' && nextSymbols.length === 0 && nextEntry.symbol_keyed && nextEntry.seed_symbol && nextEntry.suggested_symbols.length > 0) {
    nextSymbols = [nextEntry.suggested_symbols[0]];
  }
  return nextSymbols.join(', ');
}

/** Chart/Metric lookback after a provider change (slow series start with a window spanning several periods). */
function lookbackAfterSourceChange(draft: GadgetDraft, nextEntry: ProviderMetrics, providerChanged: boolean): string | undefined {
  const { renderer } = draft;
  if ((renderer === 'finance_chart' || renderer === 'provider_observation') && !draft.base && providerChanged && !draft.lookbackTouched) {
    // A Metric gadget never starts below its 30-day runtime window (same rule as useEffectiveLookback).
    return String(Math.max(nextEntry.default_lookback_days ?? 0, renderer === 'provider_observation' ? fallbackLookback(renderer) : 0) || fallbackLookback(renderer));
  }
  return undefined;
}

/**
 * Applies a datasource selection and returns the draft patch. For charts the default metric follows the new provider:
 * `close` (else the first catalog metric) is chosen whenever the current one is not offered (open catalogs keep typed
 * ids). When the provider actually changed and the new one suggests symbols, symbols it does not suggest are dropped;
 * a new chart's lookback follows the provider's catalog default, an empty chart symbol list is preselected with the
 * first suggestion, and region-keyed charts do the same for the region. Re-selecting the same provider keeps the
 * owner's symbols and region. Pure: the caller merges the returned patch.
 */
export function planSourceChange(ctx: SourceChangeContext): Partial<GadgetDraft> {
  const { next, draft, providerOf, catalog } = ctx;
  const providersOf = (ids: string[]) => [...new Set(ids.map(providerOf).filter((item): item is string => item !== null))].sort();
  const nextProviders = providersOf(next);
  const providerChanged = nextProviders.join() !== providersOf(draft.sourceIds).join() || providerOf(next[0]) !== providerOf(draft.sourceIds[0]);
  const patch: Partial<GadgetDraft> = { sourceIds: next };
  const nextEntry = mergeCatalogEntries(nextProviders.map((item) => catalog?.[item]));
  const metrics = metricsAfterSourceChange(ctx, nextProviders, nextEntry, providerChanged);
  if (metrics !== undefined) patch.metrics = metrics;
  if (!nextEntry) return patch;
  const lookback = lookbackAfterSourceChange(draft, nextEntry, providerChanged);
  if (lookback !== undefined) patch.lookback = lookback;
  const symbols = symbolsAfterSourceChange(ctx, nextProviders, nextEntry, providerChanged);
  if (symbols !== undefined) patch.symbols = symbols;
  if (draft.renderer === 'finance_chart') {
    if (!nextEntry.region_keyed) patch.region = '';
    else if (providerChanged || !draft.region) patch.region = nextEntry.suggested_regions[0] ?? '';
  }
  return patch;
}

/** Facts derived from the draft and catalog that decide what the save payload contains. */
export interface SavePayloadContext {
  draft: GadgetDraft;
  /** Merged catalog entry of the bound providers (undefined when unknown). */
  entry: ProviderMetrics | undefined;
  /** Several bound providers make the chart metric a checklist. */
  chartMulti: boolean;
  /** Template of the chosen renderer, whose scope seeds a new definition. */
  template: GadgetTemplate | undefined;
}

const sameList = (a: string[], b?: string[]) => JSON.stringify(a) === JSON.stringify(b ?? []);

/** Which stored chart facts are unchanged by the current edit; they decide whether defaults may be injected. */
function chartChangeFacts(draft: GadgetDraft) {
  const { base, sourceIds, region, symbols, metrics } = draft;
  // Unchanged source on a saved definition: stored metrics/symbols/regions go back as stored (a preset-created
  // chart may legitimately have none yet); defaults are injected only for new definitions or a changed source.
  const sourcesUnchanged = base !== null && JSON.stringify(sourceIds) === JSON.stringify(base.source_ids);
  // The region field holds one value, so a legacy chart with several stored regions is unchanged while that field still shows the first.
  const regionUnchanged = base !== null && region.trim() === (base.scope?.regions?.[0] ?? '');
  const keysChanged = base !== null && !(sameList(splitList(symbols, true), base.scope?.symbols) && regionUnchanged);
  // A legacy multi-metric chart keeps its stored metrics exactly while nothing that affects them changed.
  const metricsUnchanged = base !== null && sameList(splitList(metrics), base.scope?.metrics);
  return { sourcesUnchanged, regionUnchanged, keysChanged, metricsUnchanged };
}

/** Metric list saved for a chart: a checklist for multi-provider charts, otherwise one metric (see inline rules). */
function chartMetrics(ctx: SavePayloadContext): string[] {
  const { draft, entry, chartMulti } = ctx;
  const { sourcesUnchanged, keysChanged, metricsUnchanged } = chartChangeFacts(draft);
  const stored = splitList(draft.metrics);
  if (chartMulti) return stored;
  // A chart plots one metric: a legacy multi-metric row keeps its first one so a symbol edit still validates.
  // Without a known provider entry nothing is invented: stored metrics go back unchanged (even when empty).
  // Adding symbols/regions to a chart saved without any makes it plottable, so the default metric is injected then too.
  if (entry && (!sourcesUnchanged || keysChanged)) return [stored[0] ?? defaultMetric(entry)];
  return metricsUnchanged && sourcesUnchanged && !keysChanged ? stored : stored.slice(0, 1);
}

/** Region list saved for a chart (one region; legacy multi-region rows stay untouched while unchanged). */
function chartRegions(ctx: SavePayloadContext): string[] {
  const { draft, entry } = ctx;
  const { sourcesUnchanged, regionUnchanged } = chartChangeFacts(draft);
  if (entry && entry.region_keyed !== true) return [];
  if (regionUnchanged && sourcesUnchanged) return draft.base?.scope?.regions ?? [];
  return draft.region.trim() ? [draft.region.trim()] : [];
}

/** Scope of a finance chart: symbols, one metric (or a checklist), region and window. */
function chartScope(ctx: SavePayloadContext, days: number | undefined): GadgetScope {
  const { draft, entry } = ctx;
  return {
    symbols: entry && entry.symbol_keyed !== true ? [] : splitList(draft.symbols, true),
    metrics: chartMetrics(ctx),
    regions: chartRegions(ctx),
    lookback_days: days,
  };
}

/** Scope of a map gadget (live providers only in live mode). */
function mapScope(draft: GadgetDraft, capped: number | undefined): GadgetScope {
  const live = draft.mapMode === 'live' ? {
    live_map_providers: draft.liveProviders,
    live_map_country: draft.country.trim().toUpperCase() || undefined,
    live_map_github_owner: draft.githubOwner.trim() || undefined,
    live_map_github_repo: draft.githubRepo.trim() || undefined,
    live_map_sec_cik: draft.secCik.trim() || undefined,
  } : {};
  return { regions: splitList(draft.regions), map_layer_ids: draft.layers, lookback_days: capped, ...live };
}

/** Renderer-specific part of the scope (merged over the base/template scope). */
function rendererScope(ctx: SavePayloadContext, days: number | undefined, capped: number | undefined): GadgetScope {
  const { draft, entry } = ctx;
  switch (draft.renderer) {
    case 'finance_chart': return chartScope(ctx, days);
    // A Metric gadget on a symbol-keyed provider keeps its picked symbols; clearing them must persist, so always send the list.
    case 'provider_observation': return { metrics: splitList(draft.metrics), symbols: entry && !entry.symbol_keyed ? [] : splitList(draft.symbols, true), lookback_days: days };
    case 'weather': return { metrics: splitList(draft.metrics), lookback_days: days };
    case 'map': return mapScope(draft, capped);
    case 'intelligence_panel': return { regions: splitList(draft.regions), cii_country_codes: splitList(draft.cii, true), lookback_days: capped };
    case 'telegram_feed': return { channel_ids: splitList(draft.channels) };
    default: return splitList(draft.symbols, true).length > 0 ? { symbols: splitList(draft.symbols, true) } : {};
  }
}

/**
 * Builds the filters and scope sent to the definition API from the draft.
 *
 * Empty lookback means unset (stored null stays null); map/intelligence windows are capped to 30 days.
 */
export function buildDefinitionPayload(ctx: SavePayloadContext): { filters: GadgetFilters; scope: GadgetScope } {
  const { draft, template } = ctx;
  const keywords = splitList(draft.keywords);
  const exclude = splitList(draft.exclude);
  const filters: GadgetFilters = {
    limit: Number(draft.limit),
    keywords: keywords.length > 0 ? [...new Set(keywords)] : undefined,
    exclude_keywords: exclude.length > 0 ? [...new Set(exclude)] : undefined,
    ...(draft.renderer === 'map' ? { map_engine: draft.engine, show_precise_locations: draft.precise, map_mode: draft.mapMode, map_weather_layer: draft.weatherLayer === 'none' ? null : draft.weatherLayer } : {}),
  };
  const days = draft.lookback.trim() === '' ? undefined : Number(draft.lookback);
  const capped = days === undefined ? undefined : Math.min(30, days);
  const scope: GadgetScope = {
    ...(draft.base?.scope ?? template?.scope ?? {}),
    ...rendererScope(ctx, days, capped),
  };
  return { filters, scope };
}
