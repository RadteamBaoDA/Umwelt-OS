'use client';

import { useQuery } from '@tanstack/react-query';
import { dashboardKeys, getMetricCatalog, listGadgetSources } from '../api';

/** Window (days) a Metric gadget reads when neither the gadget nor its provider's catalog sets one; the editor seeds the same value. */
export const METRIC_FALLBACK_LOOKBACK_DAYS = 30;
/** Window (days) a chart reads when neither the gadget nor its provider's catalog sets one; the editor seeds the same value. */
export const CHART_FALLBACK_LOOKBACK_DAYS = 90;

/** Page size and page cap of the bound-provider lookup (bounds the walk at 1000 sources). */
const LOOKUP_PAGE_SIZE = 100;
const LOOKUP_MAX_PAGES = 10;

/**
 * Finds the provider id of one source by walking the source list page by page, stopping when found, when the list
 * ends, or after {@link LOOKUP_MAX_PAGES} pages. There is no by-id source read for gadgets, so this is the bounded
 * lookup; a source beyond the cap resolves to null and the caller falls back to its generic default.
 *
 * @param sourceId Source whose provider is wanted.
 * @param signal Abort signal forwarded to every page request.
 * @returns Provider id, or null when the source is not found within the bound.
 */
async function findSourceProvider(sourceId: string, signal?: AbortSignal): Promise<string | null> {
  let cursor: string | undefined;
  for (let page = 0; page < LOOKUP_MAX_PAGES; page += 1) {
    const result = await listGadgetSources(LOOKUP_PAGE_SIZE, cursor, signal);
    const match = result.items.find((item) => item.id === sourceId);
    if (match) return match.provider ?? null;
    if (!result.next_cursor) return null;
    cursor = result.next_cursor;
  }
  return null;
}

/**
 * Resolves the lookback window (days) of a gadget scope. A stored value wins; otherwise the bound provider's catalog
 * default applies (annual World Bank needs ~10 years, monthly OECD ~2), else `fallback`. The provider is only known
 * through the source list, so `ready` stays false until the lookups settle (a failed lookup falls back instead of
 * blocking the gadget).
 *
 * @param sourceId Bound source id, or empty when unbound.
 * @param stored Stored `scope.lookback_days`, or null/undefined when unset.
 * @param fallback Window used when no catalog default is known.
 * @param widenToFallback When true the catalog default never narrows below `fallback` (Metric gadgets: the 3-day chart
 *   default of a fast provider must not shrink a gadget that always read 30 days); a wider catalog default still wins.
 * @returns The effective window and whether it is final.
 */
export function useEffectiveLookback(sourceId: string, stored: number | null | undefined, fallback: number, widenToFallback = false) {
  const unset = stored === null || stored === undefined;
  const catalog = useQuery({ queryKey: dashboardKeys.metricCatalog, queryFn: ({ signal }) => getMetricCatalog(signal), staleTime: 300_000, retry: false });
  const provider = useQuery({
    queryKey: [...dashboardKeys.sources, 'chart-provider', sourceId],
    queryFn: ({ signal }) => findSourceProvider(sourceId, signal),
    enabled: Boolean(sourceId) && unset, staleTime: 300_000, retry: false,
  });
  const entry = provider.data ? catalog.data?.[provider.data] : undefined;
  const catalogDefault = entry?.default_lookback_days ?? null;
  return {
    lookbackDays: stored ?? (catalogDefault === null ? fallback : widenToFallback ? Math.max(catalogDefault, fallback) : catalogDefault),
    ready: !unset || (!provider.isPending && !catalog.isPending),
    catalog,
  };
}
