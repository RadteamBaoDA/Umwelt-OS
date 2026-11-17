import { describe, expect, it } from 'vitest';
import gadgetRenderers from '@/test/fixtures/gadget-renderers.json';
import gadgetTemplates from '@/test/fixtures/gadget-templates.json';
import { RENDERER_MIN_SIZES, RENDERER_DEFAULT_SIZES } from './grid-geometry';
import type { GadgetDefinition, ProviderMetrics } from './api';
import {
  buildDefinitionPayload,
  draftPatchForTemplate,
  initialDraft,
  isMediaDraftValid,
  fieldsFor,
  planSourceChange,
  type GadgetDraft,
} from './gadget-editor-model';

/** Builds a catalog entry with sensible defaults. */
const entry = (over: Partial<ProviderMetrics> = {}): ProviderMetrics => ({
  metrics: [
    { id: 'close', unit: 'USD' },
    { id: 'volume', unit: null },
  ],
  symbol_keyed: true,
  suggested_symbols: ['BTC', 'ETH'],
  metrics_open: false,
  charts_as_series: true,
  region_keyed: false,
  suggested_regions: [],
  seed_symbol: true,
  cadence_days: 1,
  default_lookback_days: 365,
  ...over,
});
const rows = [
  { id: 's1', provider: 'binance' },
  { id: 's2', provider: 'fred' },
];
const providerOf = (id: string | undefined) => rows.find((row) => row.id === id)?.provider ?? null;
const catalog = {
  binance: entry(),
  fred: entry({
    symbol_keyed: true,
    suggested_symbols: ['GDP'],
    seed_symbol: false,
    default_lookback_days: 3650,
    metrics: [{ id: 'value', unit: null }],
  }),
};
/** A blank new finance-chart draft. */
const chartDraft = (over: Partial<GadgetDraft> = {}): GadgetDraft => ({ ...initialDraft(null), renderer: 'finance_chart', ...over });
const fields = new Set(['symbols', 'metrics', 'lookback'] as const);

describe('planSourceChange', () => {
  it('seeds metric, symbols and lookback for a new chart bound to its first provider', () => {
    const patch = planSourceChange({ next: ['s1'], draft: chartDraft(), providerOf, catalog, fields });
    expect(patch).toMatchObject({ sourceIds: ['s1'], metrics: 'close', symbols: 'BTC', lookback: '365' });
  });

  it('drops symbols the new provider does not suggest and keeps a typed lookback', () => {
    const draft = chartDraft({ sourceIds: ['s1'], symbols: 'BTC, XRP', metrics: 'close', lookbackTouched: true, lookback: '30' });
    const patch = planSourceChange({ next: ['s2'], draft, providerOf, catalog, fields });
    expect(patch.symbols).toBe('');
    expect(patch.metrics).toBe('value');
    expect(patch).not.toHaveProperty('lookback');
  });

  it('keeps the owner symbols when the provider does not change', () => {
    const draft = chartDraft({ sourceIds: ['s1'], symbols: 'BTC, XRP', metrics: 'close' });
    const patch = planSourceChange({ next: ['s1'], draft, providerOf, catalog, fields });
    expect(patch.symbols).toBe('BTC, XRP');
  });

  it('only records the selection when the provider catalog is unknown', () => {
    expect(planSourceChange({ next: ['zzz'], draft: chartDraft(), providerOf, catalog, fields })).toEqual({ sourceIds: ['zzz'] });
  });

  it('adds one default metric per provider for multi-provider charts', () => {
    const patch = planSourceChange({ next: ['s1', 's2'], draft: chartDraft({ metrics: 'close' }), providerOf, catalog, fields });
    expect(patch.metrics).toBe('close, value');
  });
});

describe('draftPatchForTemplate', () => {
  it('resets every configuration field for the picked renderer', () => {
    const patch = draftPatchForTemplate(undefined, 'news_feed', 'News');
    expect(patch).toMatchObject({
      choice: 'news_feed',
      renderer: 'news_feed',
      name: 'News',
      sourceIds: [],
      limit: '25',
      mapMode: 'live',
      rules: [],
      region: '',
    });
  });
});

describe('buildDefinitionPayload', () => {
  it('builds list filters with de-duplicated keywords', () => {
    const draft = { ...initialDraft(null), renderer: 'news_feed', keywords: 'a, b, a', exclude: '', limit: '10' };
    const { filters, scope } = buildDefinitionPayload({ draft, entry: undefined, chartMulti: false, template: undefined });
    expect(filters).toMatchObject({ limit: 10, keywords: ['a', 'b'], exclude_keywords: undefined });
    expect(scope).toEqual({});
  });

  it('caps map lookback at 30 and includes live-map settings only in live mode', () => {
    const draft = { ...initialDraft(null), renderer: 'map', lookback: '90', country: ' vn ', mapMode: 'live' as const };
    const { filters, scope } = buildDefinitionPayload({ draft, entry: undefined, chartMulti: false, template: undefined });
    expect(scope).toMatchObject({ lookback_days: 30, live_map_country: 'VN', map_layer_ids: [] });
    expect(filters).toMatchObject({ map_mode: 'live', map_weather_layer: null });
    const selected = buildDefinitionPayload({
      draft: { ...draft, mapMode: 'selected_sources' },
      entry: undefined,
      chartMulti: false,
      template: undefined,
    });
    expect(selected.scope).not.toHaveProperty('live_map_providers');
  });

  it('returns stored chart metrics unchanged for an unchanged saved definition', () => {
    const definition = {
      id: 'd',
      name: 'Chart',
      revision: 2,
      renderer: 'finance_chart',
      source_ids: ['s1'],
      highlight_rules: [],
      config_version: 1,
      runtime_state: 'available',
      scope: { symbols: ['BTC'], metrics: ['close', 'volume'], regions: [], lookback_days: 90 },
      filters: { limit: 25 },
    } as unknown as GadgetDefinition;
    const draft = initialDraft(definition);
    const { scope } = buildDefinitionPayload({ draft, entry: undefined, chartMulti: false, template: undefined });
    expect(scope.metrics).toEqual(['close', 'volume']);
    expect(scope.lookback_days).toBe(90);
  });

  it('keeps only the first chart metric when a symbol edit changes the keys', () => {
    const definition = {
      id: 'd',
      name: 'Chart',
      revision: 2,
      renderer: 'finance_chart',
      source_ids: ['s1'],
      highlight_rules: [],
      config_version: 1,
      runtime_state: 'available',
      scope: { symbols: ['BTC'], metrics: ['close', 'volume'], regions: [] },
      filters: { limit: 25 },
    } as unknown as GadgetDefinition;
    const draft = { ...initialDraft(definition), symbols: 'BTC, ETH' };
    const { scope } = buildDefinitionPayload({ draft, entry: entry(), chartMulti: false, template: undefined });
    expect(scope.metrics).toEqual(['close']);
    expect(scope.symbols).toEqual(['BTC', 'ETH']);
  });
});

describe('media editor contracts', () => {
  it('keeps media geometry aligned with renderer and template snapshots', () => {
    for (const [renderer, template] of [
      ['media_feed', 'media_shelf'],
      ['media_calendar', 'release_calendar'],
    ]) {
      const descriptor = gadgetRenderers.find((row) => row.id === renderer)!;
      expect(RENDERER_MIN_SIZES[renderer]).toEqual({ minW: descriptor.minimum_width, minH: descriptor.minimum_height });
      expect(gadgetTemplates.find((row) => row.id === template)?.renderer).toBe(renderer);
    }
    expect(RENDERER_DEFAULT_SIZES.media_feed).toEqual({ defaultW: 8, defaultH: 6 });
    expect(RENDERER_DEFAULT_SIZES.media_calendar).toEqual({ defaultW: 6, defaultH: 6 });
  });
  it('exposes media defaults only for media renderers and the window for calendars', () => {
    expect([...fieldsFor('media_feed')]).toEqual(expect.arrayContaining(['keywords', 'limit', 'rules', 'mediaScope', 'mediaFilters']));
    expect(fieldsFor('media_calendar').has('releaseWindow')).toBe(true);
    expect(fieldsFor('media_feed').has('releaseWindow')).toBe(false);
    expect(fieldsFor('news_feed').has('mediaScope')).toBe(false);
  });

  it('round-trips selectors and all filter defaults while permitting clears', () => {
    const definition: GadgetDefinition = {
      id: 'media',
      name: 'Shelf',
      renderer: 'media_calendar',
      source_ids: ['s1'],
      revision: 1,
      scope: {
        media_types: ['anime'],
        countries: ['JP'],
        genres: ['sci_fi'],
        languages: ['ja'],
        formats: ['tv'],
        content_ratings: ['safe'],
        release_year: 2026,
        release_season: 'fall',
        min_rating: 80,
        release_window_days: 14,
      },
      filters: {
        media_type: 'anime',
        media_types: ['anime'],
        release_status: 'upcoming',
        group_by: 'date',
        countries: ['JP'],
        genres: ['action'],
        languages: ['ja'],
        formats: ['tv'],
        content_ratings: ['safe'],
        min_rating: 60,
        release_year: 2026,
        release_season: 'fall',
        limit: 25,
      },
      highlight_rules: [],
      config_version: 1,
      runtime_state: 'available',
    };
    const draft = initialDraft(definition);
    expect(draft.minRating).toBe('60');
    expect(draft.mediaScope.genres).toEqual(['action']);
    const context = { draft, entry: undefined, chartMulti: false, template: undefined };
    expect(buildDefinitionPayload(context)).toEqual({
      scope: definition.scope,
      filters: { ...definition.filters, keywords: undefined, exclude_keywords: undefined },
    });
    const cleared = buildDefinitionPayload({
      ...context,
      draft: { ...draft, mediaScope: { ...draft.mediaScope, countries: [] }, releaseYear: '', minRating: '', releaseWindow: '' },
    });
    expect(cleared.scope).toMatchObject({ countries: [], release_year: null, min_rating: null, release_window_days: null });
    expect(cleared.filters).toMatchObject({ ...definition.filters, countries: [], release_year: null, min_rating: null });
  });

  it.each([
    ['releaseYear', '1799'],
    ['releaseYear', '2101'],
    ['releaseYear', '2026.5'],
    ['minRating', '-1'],
    ['minRating', '101'],
    ['minRating', 'Infinity'],
    ['releaseWindow', '0'],
    ['releaseWindow', '91'],
    ['releaseWindow', '1.5'],
  ])('rejects invalid %s value %s', (key, value) => {
    expect(isMediaDraftValid({ ...initialDraft(null), renderer: 'media_calendar', [key]: value })).toBe(false);
  });

  it('accepts optional values and bounds, and resets media state when changing templates', () => {
    expect(isMediaDraftValid({ ...initialDraft(null), renderer: 'media_calendar' })).toBe(true);
    expect(
      isMediaDraftValid({ ...initialDraft(null), renderer: 'media_calendar', releaseYear: '1800', minRating: '0', releaseWindow: '90' }),
    ).toBe(true);
    expect(draftPatchForTemplate(undefined, 'news_feed', 'News')).toMatchObject({
      mediaScope: { countries: [], genres: [] },
      mediaFilters: {},
      releaseYear: '',
      minRating: '',
      releaseWindow: '',
    });
  });
});
