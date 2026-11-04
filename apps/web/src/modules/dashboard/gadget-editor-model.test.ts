import { describe, expect, it } from 'vitest';
import type { GadgetDefinition, ProviderMetrics } from './api';
import {
  buildDefinitionPayload, draftPatchForTemplate, initialDraft, planSourceChange, type GadgetDraft,
} from './gadget-editor-model';

/** Builds a catalog entry with sensible defaults. */
const entry = (over: Partial<ProviderMetrics> = {}): ProviderMetrics => ({
  metrics: [{ id: 'close', unit: 'USD' }, { id: 'volume', unit: null }], symbol_keyed: true, suggested_symbols: ['BTC', 'ETH'],
  metrics_open: false, charts_as_series: true, region_keyed: false, suggested_regions: [], seed_symbol: true,
  cadence_days: 1, default_lookback_days: 365, ...over,
});
const rows = [{ id: 's1', provider: 'binance' }, { id: 's2', provider: 'fred' }];
const providerOf = (id: string | undefined) => rows.find((row) => row.id === id)?.provider ?? null;
const catalog = { binance: entry(), fred: entry({ symbol_keyed: true, suggested_symbols: ['GDP'], seed_symbol: false, default_lookback_days: 3650, metrics: [{ id: 'value', unit: null }] }) };
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
    expect(patch).toMatchObject({ choice: 'news_feed', renderer: 'news_feed', name: 'News', sourceIds: [], limit: '25', mapMode: 'live', rules: [], region: '' });
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
    const selected = buildDefinitionPayload({ draft: { ...draft, mapMode: 'selected_sources' }, entry: undefined, chartMulti: false, template: undefined });
    expect(selected.scope).not.toHaveProperty('live_map_providers');
  });

  it('returns stored chart metrics unchanged for an unchanged saved definition', () => {
    const definition = {
      id: 'd', name: 'Chart', revision: 2, renderer: 'finance_chart', source_ids: ['s1'], highlight_rules: [], config_version: 1, runtime_state: 'available',
      scope: { symbols: ['BTC'], metrics: ['close', 'volume'], regions: [], lookback_days: 90 }, filters: { limit: 25 },
    } as unknown as GadgetDefinition;
    const draft = initialDraft(definition);
    const { scope } = buildDefinitionPayload({ draft, entry: undefined, chartMulti: false, template: undefined });
    expect(scope.metrics).toEqual(['close', 'volume']);
    expect(scope.lookback_days).toBe(90);
  });

  it('keeps only the first chart metric when a symbol edit changes the keys', () => {
    const definition = {
      id: 'd', name: 'Chart', revision: 2, renderer: 'finance_chart', source_ids: ['s1'], highlight_rules: [], config_version: 1, runtime_state: 'available',
      scope: { symbols: ['BTC'], metrics: ['close', 'volume'], regions: [] }, filters: { limit: 25 },
    } as unknown as GadgetDefinition;
    const draft = { ...initialDraft(definition), symbols: 'BTC, ETH' };
    const { scope } = buildDefinitionPayload({ draft, entry: entry(), chartMulti: false, template: undefined });
    expect(scope.metrics).toEqual(['close']);
    expect(scope.symbols).toEqual(['BTC', 'ETH']);
  });
});
