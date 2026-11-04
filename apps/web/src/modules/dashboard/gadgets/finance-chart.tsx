'use client';

import React from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import type { GadgetInstance } from '../api';
import { formatDateTime, type AppLocaleId } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow } from '@/modules/observations';
import { CHART_FALLBACK_LOOKBACK_DAYS, useEffectiveLookback } from './use-effective-lookback';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { MetricsChart, type ChartSeriesData, type MetricsChartProps } from './metrics-chart';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { formatProviderName } from './metric-label';

/** Props for the FinanceChart gadget component. */
export interface FinanceChartProps extends Omit<MetricsChartProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Chart gadget: one bound source renders the single-series chart, 2+ sources (up to the backend ceiling of 5) the
 * multi-series chart. Splitting here keeps each body's hook order fixed.
 *
 * @param props Gadget instance configuration and chart options.
 * @returns Accessible finance chart component.
 */
export function FinanceChart(props: FinanceChartProps) {
  return (props.instance.definition.source_ids?.length ?? 0) > 1 ? <MultiFinanceChart {...props} /> : <SingleFinanceChart {...props} />;
}

/** Observation row type shared by the chart bodies. */
type Observation = Awaited<ReturnType<typeof listWorldObservationWindow>>['items'][number];

/**
 * Groups observation rows into one series per source, metric, symbol and region, labelled "<source> · <series>".
 * Rows are ordered by observation time; the last row decides currency/unit. Pure.
 */
function buildSeriesData(items: Observation[], sources: ReturnType<typeof useEffectiveLookback>['sources']): ChartSeriesData[] {
  const groups = new Map<string, { name: string; rows: Observation[] }>();
  for (const item of items) {
    const key = [item.source_id, item.metric, item.symbol ?? '', item.region ?? ''].join('|');
    const source = sources[item.source_id];
    const sourceName = source?.name || (item.provider ? formatProviderName(item.provider) : item.source_id);
    const label = item.symbol || item.region || item.metric;
    const group = groups.get(key) ?? { name: `${sourceName} · ${label}`, rows: [] };
    group.rows.push(item);
    groups.set(key, group);
  }
  return [...groups.entries()].map(([key, group]) => {
    const rows = group.rows.sort((left, right) => left.observed_at.localeCompare(right.observed_at));
    const last = rows[rows.length - 1];
    return { key, name: group.name, points: rows.map((row) => ({ timestamp: row.observed_at, value: row.value as number })), currency: last.currency ?? undefined, unit: last.currency ? undefined : (last.unit ?? undefined) };
  });
}

/**
 * One observation query per bound source (a shared request would let symbol-less rows such as Fear & Greed drop out
 * of a `symbols` filter and share one row budget). `symbols` apply only to symbol-keyed providers and `regions` only
 * to region-keyed ones (unknown providers get both). Reports the newest collection time as freshness.
 */
function useMultiFinanceData(instance: GadgetInstance) {
  const definition = instance.definition;
  const sourceIds = definition.source_ids ?? [];
  const symbols = definition.scope?.symbols ?? [];
  const regions = definition.scope?.regions ?? [];
  const metrics = definition.scope?.metrics?.length ? definition.scope.metrics : ['close'];
  const { lookbackDays, ready: lookbackReady, catalog, sources } = useEffectiveLookback(sourceIds, definition.scope?.lookback_days, CHART_FALLBACK_LOOKBACK_DAYS);
  const catalogReady = !catalog.isPending;
  const queries = useQueries({
    queries: sourceIds.map((sourceId) => {
      const entry = sources[sourceId]?.provider ? catalog.data?.[sources[sourceId].provider as string] : undefined;
      const useSymbols = symbols.length > 0 && (!entry || entry.symbol_keyed);
      const useRegions = regions.length > 0 && (!entry || entry.region_keyed);
      return {
        queryKey: ['world-observations', 'finance', instance.id, sourceId, useSymbols ? symbols : null, useRegions ? regions : null, metrics, lookbackDays],
        queryFn: ({ signal }: { signal: AbortSignal }) => {
          const to = new Date(Date.now() + 86_400_000);
          const from = new Date(to.getTime() - Math.min(lookbackDays + 1, 3660) * 86_400_000);
          return listWorldObservationWindow({ sourceIds: [sourceId], symbols: useSymbols ? symbols : undefined, regions: useRegions ? regions : undefined, metrics, from, to, history: true }, signal);
        },
        enabled: lookbackReady && catalogReady, staleTime: 60_000,
      };
    }),
  });
  const items = queries.flatMap((query, index) => (query.data?.items ?? []).filter((item) => item.source_id === sourceIds[index] && item.value !== null));
  const latest = items.reduce<Observation | undefined>((best, item) => (!best || item.collected_at > best.collected_at ? item : best), undefined);
  useReportGadgetFreshness(latest?.collected_at, null);
  return { queries, items, sources, ready: lookbackReady && catalogReady };
}

/**
 * Multi-source Chart body: one observation query per bound source. The scope is one union: metrics are
 * matched per row. Series are keyed by source, symbol and region and labelled "<source> · <series>"; MetricsChart
 * rebases them to percent change when their units differ.
 * ponytail: a symbol chosen for source B also shows on source A when A emits it; symbols cannot be mapped to sources.
 *
 * @param props Gadget instance configuration and chart options.
 * @returns Multi-series chart, or the setup/loading/error/empty notice.
 */
function MultiFinanceChart(props: FinanceChartProps) {
  const t = useTranslations('dashboard');
  const { queries, items, sources, ready } = useMultiFinanceData(props.instance);
  // Recomputed per render (cheap, <= 5 sources); MetricsChart memoizes on the series identity it receives.
  const seriesData = buildSeriesData(items, sources);
  if (queries.some((query) => query.isPending) || !ready) return <GadgetSkeleton variant="chart" label={t('observationLoading')} />;
  if (queries.every((query) => query.isError)) return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (seriesData.length === 0) return <GadgetNotice kind="empty" message={t('observationEmpty')} />;
  return <>
    {queries.some((query) => query.data?.truncated) && <p role="status" className="px-3 pt-2 text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <MetricsChart {...props} seriesData={seriesData} />
  </>;
}

/** What the single-source chart shows, decided from the loaded data in the original check order. */
type SingleChartState = 'setup' | 'empty' | 'loading' | 'error' | 'mixedUnits' | 'chart';

/**
 * Series key of one observation row: a line is one series. Region-keyed providers, point providers
 * (`charts_as_series` false, e.g. USGS quakes where the region is a per-event place) and unknown providers (catalog
 * missing or failed) split by symbol and region so nothing merges silently; other providers split by symbol only.
 */
function observationSeriesKey(item: Observation, catalog: ReturnType<typeof useEffectiveLookback>['catalog']): string {
  const entry = item.provider ? catalog.data?.[item.provider] : undefined;
  return !entry || entry.region_keyed || !entry.charts_as_series ? `${item.symbol ?? ''}|${item.region ?? ''}` : (item.symbol ?? '');
}

/** Facts that decide the single-source chart state. */
interface SingleChartFacts {
  sourceId: string;
  symbol: string;
  hasMetrics: boolean;
  success: boolean;
  pending: boolean;
  error: boolean;
  rows: Observation[];
  comparable: boolean;
  catalog: ReturnType<typeof useEffectiveLookback>['catalog'];
}

/** True when the owner still has to choose a source or series (see the inline reasons). */
function needsSeriesSetup(f: SingleChartFacts): boolean {
  const { rows, catalog } = f;
  const mixedSeries = new Set(rows.map((row) => observationSeriesKey(row, catalog))).size > 1;
  // A scope with no metric defaults to `close`; keyed providers (FRED, Binance, OpenAQ...) have none, so an empty
  // result means the series is still to be chosen rather than "no data".
  const unseeded = !f.hasMetrics && !f.symbol && f.success && rows.length === 0;
  // A symbol-keyed provider (same rule as backend readiness `scope_required`) with no configured symbol never draws
  // whichever single series happens to exist: the owner must choose one, so the result does not depend on how many exist.
  const symbolRequired = !f.symbol && f.success && rows.length > 0 && rows.every((item) => item.provider && catalog.data?.[item.provider]?.symbol_keyed === true);
  return !f.sourceId || unseeded || symbolRequired || (f.success && mixedSeries);
}

/** Picks what to show: setup notice, empty/loading/error states, mixed units or the chart (first match wins). */
function classifySingleChart(f: SingleChartFacts): SingleChartState {
  if (needsSeriesSetup(f)) return 'setup';
  // A legacy chart (no metrics, symbol set) plots `close`; no rows there is plain no-data, not an unchosen series.
  if (!f.hasMetrics && f.success && f.rows.length === 0) return 'empty';
  if (f.pending || f.catalog.isPending) return 'loading';
  if (f.error) return 'error';
  return f.comparable ? 'chart' : 'mixedUnits';
}

/** First source, symbol, region and metric of the scope (metric defaults to `close`). */
function readSingleScope(definition: GadgetInstance['definition']) {
  return {
    sourceId: definition.source_ids?.[0] ?? '',
    symbol: definition.scope?.symbols?.[0] ?? '',
    region: definition.scope?.regions?.[0] ?? '',
    metric: definition.scope?.metrics?.[0] ?? 'close',
  };
}

/** Frame footer text: provider name and delay in seconds, or null when there is nothing to say. */
function footerAttribution(selected: Observation | undefined): string | null {
  if (!selected) return null;
  const parts = [selected.provider ? formatProviderName(selected.provider) : null, selected.provider_delay_seconds == null ? null : `${selected.provider_delay_seconds}s`];
  return parts.filter(Boolean).join(' · ') || null;
}

/**
 * Loads the single source's observations (filtered by symbol, region and metric, oldest first) and reports provider
 * name and delay as the frame footer.
 */
function useSingleFinanceData(instance: GadgetInstance) {
  const definition = instance.definition;
  const { sourceId, symbol, region, metric } = readSingleScope(definition);
  // No stored lookback: start from the bound provider's catalog default (annual World Bank needs ~10 years, monthly
  // OECD ~2), else 90 days; the chart waits for that lookup (a failed one falls back to 90 rather than blocking).
  const { lookbackDays, ready: lookbackReady, catalog } = useEffectiveLookback(sourceId, definition.scope?.lookback_days, CHART_FALLBACK_LOOKBACK_DAYS);
  const observations = useQuery({
    queryKey: ['world-observations', 'finance', instance.id, sourceId, symbol, region, metric, lookbackDays],
    queryFn: ({ signal }) => {
      // `to` runs one day ahead so a provider clock slightly in the future is not cut off; the window stays within the API's 3660-day maximum.
      const to = new Date(Date.now() + 86_400_000);
      const from = new Date(to.getTime() - Math.min(lookbackDays + 1, 3660) * 86_400_000);
      return listWorldObservationWindow({ sourceIds: [sourceId], symbols: symbol ? [symbol] : undefined, regions: region ? [region] : undefined, metrics: [metric], from, to, history: true }, signal);
    },
    enabled: Boolean(sourceId) && lookbackReady, staleTime: 60_000,
  });
  const rows = (observations.data?.items ?? [])
    .filter((item) => item.source_id === sourceId && (!symbol || item.symbol === symbol) && (!region || item.region === region) && item.metric === metric && item.value !== null)
    .sort((left, right) => left.observed_at.localeCompare(right.observed_at));
  const selected = rows.at(-1);
  // Provider delay and name go to the frame footer instead of the chart header.
  useReportGadgetFreshness(selected?.collected_at, footerAttribution(selected));
  const comparable = rows.every((item) => item.unit === selected?.unit && item.currency === selected?.currency);
  return { sourceId, symbol, hasMetrics: Boolean(definition.scope?.metrics?.length), observations, rows, selected, comparable, catalog };
}

/** Provider delay and collection time text passed to the chart as its delay notice. */
function providerDelayText(selected: Observation | undefined, t: ReturnType<typeof useTranslations>, locale: AppLocaleId, timezone: string): string | undefined {
  if (!selected) return undefined;
  const delay = selected.provider_delay_seconds == null ? '' : `${selected.provider_delay_seconds}s · `;
  const provider = selected.provider ? ` · ${formatProviderName(selected.provider)}` : '';
  return `${delay}${t('observationCollectedAt')} ${formatDateTime(selected.collected_at, locale, selected.timezone ?? timezone)}${provider}`;
}

/**
 * Standard Chart gadget template (one bound source).
 * Specializes MetricsChart for any measurement series. Rows are filtered by the configured symbol and region. The
 * series key is the symbol, plus the region for region-keyed providers (World Bank countries, OpenAQ locations) per the
 * metric catalog (symbol and region when the catalog entry is unknown), so several series never merge into one
 * line; with more than one left, or a symbol-keyed provider without a configured symbol, the "select a series" state is shown. The catalog is only read to choose that key,
 * a failed lookup falls back to symbol and region.
 *
 * @param props Gadget instance configuration and chart options.
 * @returns Accessible finance chart component.
 */
function SingleFinanceChart(props: FinanceChartProps) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { sourceId, symbol, hasMetrics, observations, rows, selected, comparable, catalog } = useSingleFinanceData(props.instance);
  const state = classifySingleChart({
    sourceId, symbol, hasMetrics, success: observations.isSuccess, pending: observations.isPending, error: observations.isError, rows, comparable, catalog,
  });
  if (state === 'setup') return <GadgetNotice kind="setup" message={t('financeSourceSymbolRequired')} action={{ href: '/settings/sources', label: t('gadgetConfigureAction') }} />;
  if (state === 'empty') return <GadgetNotice kind="empty" message={t('observationEmpty')} />;
  if (state === 'loading') return <GadgetSkeleton variant="chart" label={t('observationLoading')} />;
  if (state === 'error') return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (state === 'mixedUnits') return <GadgetNotice kind="empty" message={t('observationMixedUnits')} />;
  const points = rows.flatMap((item) => item.value === null ? [] : [{ timestamp: item.observed_at, value: item.value }]);
  return <>
    {observations.data?.truncated && <p role="status" className="px-3 pt-2 text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <MetricsChart {...props} initialData={points} currency={selected?.currency ?? undefined} unit={selected?.currency ? undefined : selected?.unit} providerDelay={providerDelayText(selected, t, display.locale, display.timezone)} />
  </>;
}
