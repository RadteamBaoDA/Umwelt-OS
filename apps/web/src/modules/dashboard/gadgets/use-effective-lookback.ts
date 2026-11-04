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

/** Provider id and display name of one bound source. */
export type SourceInfo = { provider: string | null; name: string };

/**
 * Finds the provider and name of the wanted sources by walking the source list page by page, stopping when all are
 * found, when the list ends, or after {@link LOOKUP_MAX_PAGES} pages. There is no by-id source read for gadgets, so
 * this is the bounded lookup; a source beyond the cap is absent from the result and callers fall back to their
 * generic default.
 *
 * @param sourceIds Sources whose provider/name are wanted.
 * @param signal Abort signal forwarded to every page request.
 * @returns Info by source id for the sources found within the bound.
 */
async function findSourceInfo(sourceIds: readonly string[], signal?: AbortSignal): Promise<Record<string, SourceInfo>> {
  const wanted = new Set(sourceIds);
  const found: Record<string, SourceInfo> = {};
  let cursor: string | undefined;
  for (let page = 0; page < LOOKUP_MAX_PAGES && Object.keys(found).length < wanted.size; page += 1) {
    const result = await listGadgetSources(LOOKUP_PAGE_SIZE, cursor, signal);
    for (const item of result.items) if (wanted.has(item.id)) found[item.id] = { provider: item.provider ?? null, name: item.name ?? '' };
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
  }
  return found;
}

/**
 * Looks up provider and name of bound sources (bounded paged walk, cached 5 minutes, no retry).
 *
 * @param ids Source ids; an empty list disables the query.
 * @returns Query whose data maps each found source id to its {@link SourceInfo}.
 */
export function useSourceInfo(ids: readonly string[]) {
  return useQuery({
    queryKey: [...dashboardKeys.sources, 'chart-provider', ...ids],
    queryFn: ({ signal }) => findSourceInfo(ids, signal),
    enabled: ids.length > 0, staleTime: 300_000, retry: false,
  });
}

/**
 * Resolves the lookback window (days) of a gadget scope. A stored value wins; otherwise the bound provider's catalog
 * default applies (annual World Bank needs ~10 years, monthly OECD ~2), else `fallback`. The provider is only known
 * through the source list, so `ready` stays false until the lookups settle (a failed lookup falls back instead of
 * blocking the gadget).
 *
 * @param sourceId Bound source id (or ids for a multi-source gadget: the widest catalog default wins), or empty when unbound.
 * @param stored Stored `scope.lookback_days`, or null/undefined when unset.
 * @param fallback Window used when no catalog default is known.
 * @param widenToFallback When true the catalog default never narrows below `fallback` (Metric gadgets: the 3-day chart
 *   default of a fast provider must not shrink a gadget that always read 30 days); a wider catalog default still wins.
 * @returns The effective window, whether it is final, the metric catalog query and the bound sources' info by id.
 */
export function useEffectiveLookback(sourceId: string | readonly string[], stored: number | null | undefined, fallback: number, widenToFallback = false) {
  const unset = stored === null || stored === undefined;
  const catalog = useQuery({ queryKey: dashboardKeys.metricCatalog, queryFn: ({ signal }) => getMetricCatalog(signal), staleTime: 300_000, retry: false });
  const ids = typeof sourceId === 'string' ? (sourceId ? [sourceId] : []) : sourceId;
  const info = useSourceInfo(ids);
  // Several sources: the slowest provider's default wins so every series has enough history.
  const defaults = Object.values(info.data ?? {}).flatMap((item) => (item.provider ? [catalog.data?.[item.provider]?.default_lookback_days ?? null] : []));
  const known = defaults.filter((value): value is number => value !== null);
  const catalogDefault = known.length > 0 ? Math.max(...known) : null;
  return {
    lookbackDays: stored ?? (catalogDefault === null ? fallback : widenToFallback ? Math.max(catalogDefault, fallback) : catalogDefault),
    // Several sources always wait for the lookup: per-source symbol/region filters depend on each provider's catalog entry.
    ready: (!unset && ids.length < 2) || (!info.isPending && !catalog.isPending),
    catalog,
    sources: info.data ?? {},
  };
}
