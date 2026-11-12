'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiRequest } from '@/core/api';
import type { components } from '@/core/api-types.gen';
import { isLiveFeedTeardownStarted } from '@/core/live-feed-storage';
import { useWorkspace } from '@/core/workspace-context';
import type { GadgetScope } from '../api';
import { liveMapPoints } from '../live-map-data';
import type { WorkerOutbound } from './runner';
import { isBrowserProvider, providerTags } from './sources';
import { subscribeWorker, type PoolWorker } from './worker-pool';
import { filterEvents, loadEvents, toStored, upsertEvents, type StoredEvent } from './store';

/** Providers enabled when a gadget defines none. */
export const DEFAULT_PROVIDERS = [
  'usgs',
  'eonet',
  'gdacs',
  'nws_alerts',
  'hackernews',
  'lobsters',
  'cisa_kev',
  'nvd',
  'business',
  'fed',
  'launches',
  'coingecko',
];

/** Live-map config response (generated from the OpenAPI schema): revision, credential flags and the terms catalog. */
export type LiveConfig = components['schemas']['LiveConfigResponse'];

/**
 * Last known fetch state of one provider, from either path; `error` is already a translation key. `staleSince` is the
 * last successful fetch (ISO) when a failed refetch left older rows on screen.
 */
export type ProviderStatus = { fetchedAt?: string; error?: LiveErrorKey; truncated?: boolean; staleSince?: string };

/** Translation keys a provider failure can map to; response bodies are never rendered. */
export type LiveErrorKey =
  | 'liveMapTermsRequired'
  | 'liveMapConfigChanged'
  | 'liveMapFirmsSetup'
  | 'liveMapGithubSetup'
  | 'liveMapSecSetup'
  | 'liveMapSecContact'
  | 'liveMapRateLimited'
  | 'liveMapOwnerOnly'
  | 'liveMapStorageError'
  | 'liveMapSourceError';

type LiveResponse = components['schemas']['LiveMapEventsResponse'];

/** True when the config loaded without error and the owner accepted the provider's terms. */
export function termsAccepted(config: { isError: boolean; data?: LiveConfig }, id: string): boolean {
  return !config.isError && Boolean(config.data?.terms_catalog.some((term) => term.id === id && term.accepted));
}

/** The query state `snapshotProviders` reads: a failed refetch may still carry older `data`. */
export type SnapshotResult = { isError?: boolean; data?: { truncated?: boolean } };

/**
 * Backend-proxied providers whose response is the complete current set, so stored rows it no longer returns may be
 * deleted. Missing ids default to false (`=== true` in `snapshotProviders`), so a new proxied provider never prunes.
 * Every backend provider is false today, because none returns a bounded full state:
 * - firms: rolling hotspot window; lobsters: front-page top-N; cisa_kev: newest-N slice of the KEV catalog;
 * - business, bbc_world, aljazeera, vnexpress_world, vnexpress_news, gaming_news: rolling news windows;
 * - fed: recent releases only; sec: recent EDGAR filings only.
 * Flip an entry to true only when the backend returns every active item and the response is never capped.
 */
export const PROXIED_SNAPSHOT: Readonly<Record<string, boolean>> = {
  firms: false,
  lobsters: false,
  cisa_kev: false,
  business: false,
  fed: false,
  bbc_world: false,
  aljazeera: false,
  vnexpress_world: false,
  vnexpress_news: false,
  gaming_news: false,
  sec: false,
};

/**
 * Proxied providers whose response may replace their stored rows: flagged as a current-set snapshot in `snapshot`,
 * included in the write (`included`, the same predicate that builds `proxyEntries`), not errored (a failed refetch keeps
 * old data that is no longer current), loaded and not truncated. A truncated response omits rows that still exist
 * upstream, so deleting the stored rows it lacks would drop live events.
 * @param included Inclusion flags aligned with `proxied`; a provider left out of the write must not be pruned.
 * @param snapshot Per-provider current-set flags; defaults to `PROXIED_SNAPSHOT`.
 * @returns Provider ids safe to treat as complete snapshots.
 */
export function snapshotProviders(
  proxied: readonly string[],
  results: readonly SnapshotResult[],
  included: readonly boolean[],
  snapshot: Readonly<Record<string, boolean>> = PROXIED_SNAPSHOT,
): string[] {
  return proxied.filter((provider, index) => {
    const result = results[index];
    return Boolean(snapshot[provider] === true && included[index] && result && !result.isError && result.data && !result.data.truncated);
  });
}

/**
 * Whether a proxied provider's query result is written to the store. Owner-only path, accepted terms, and not a 409:
 * the server refused the scope, so any retained data is not part of the current feed. Both the entries and the
 * snapshot list use this predicate, so a provider cannot be pruned without its rows being replaced.
 * @param result The query outcome; only `error` is read.
 * @param accepted Whether the owner accepted the provider's terms.
 * @param isOwner Whether the viewer may read the owner-only proxy.
 * @returns True when the provider's rows belong in the store write.
 */
export function proxyIncluded(result: { error: unknown }, accepted: boolean, isOwner: boolean): boolean {
  return isOwner && accepted && !(result.error instanceof ApiError && result.error.status === 409);
}

/** Merge row lists by key; later lists win (stored < worker memory < fresh proxy rows). */
export function mergeEvents(...lists: StoredEvent[][]): StoredEvent[] {
  const merged = new Map<string, StoredEvent>();
  for (const list of lists) for (const row of list) merged.set(row.key, row);
  return [...merged.values()];
}

/** Fetch one provider through the workspace-scoped proxy; the signal cancels unobserved or switched requests. */
async function fetchLiveEvents(provider: string, scope: GadgetScope, signal: AbortSignal): Promise<LiveResponse> {
  const params = new URLSearchParams({ provider });
  if (provider === 'firms' && scope.live_map_country) params.set('country', scope.live_map_country);
  if (provider === 'sec' && scope.live_map_sec_cik) params.set('cik', scope.live_map_sec_cik);
  return apiRequest<LiveResponse>(`/api/v1/live-map/events?${params}`, { signal });
}

/**
 * Translate a failure into an allowlisted message key: server error codes (ApiError) or the worker's short codes
 * (`scope`, `http_429`, ...). Never renders provider response bodies or secrets.
 */
export function liveErrorKey(error: unknown, provider: string): LiveErrorKey {
  if (typeof error === 'string') {
    if (error === 'storage') return 'liveMapStorageError';
    if (error === 'http_429') return 'liveMapRateLimited';
    return error === 'scope' && provider === 'github_releases' ? 'liveMapGithubSetup' : 'liveMapSourceError';
  }
  if (!(error instanceof ApiError)) return 'liveMapSourceError';
  if (error.message === 'provider_terms_ineligible') return 'liveMapTermsRequired';
  if (error.message === 'live_map_configuration_changed') return 'liveMapConfigChanged';
  if (error.message === 'contact_required') return 'liveMapSecContact';
  if (error.message === 'provider_scope_invalid')
    return provider === 'firms' ? 'liveMapFirmsSetup' : provider === 'sec' ? 'liveMapSecSetup' : 'liveMapSourceError';
  if (error.status === 429) return 'liveMapRateLimited';
  return 'liveMapSourceError';
}

/**
 * Subscribes to the module worker shared per workspace (see worker-pool.ts): contributes the accepted browser
 * providers and scope to the merged config, mirrors gadget visibility (`active`) and releases on unmount.
 * @returns Worker-reported statuses, in-memory rows for providers whose store write failed, a counter that increments whenever the worker wrote new rows, `bump` to force a store re-read, and `refresh` to ask the worker to fetch now.
 */
function useBrowserWorker(workspaceId: string | undefined, browserKey: string, scope: GadgetScope, active: boolean) {
  const subRef = useRef<ReturnType<typeof subscribeWorker> | null>(null);
  const [workerStatus, setWorkerStatus] = useState<Record<string, ProviderStatus>>({});
  const [version, setVersion] = useState(0);
  // Rows the worker could not persist, kept in memory per provider until a later write succeeds.
  const [workerItems, setWorkerItems] = useState<Record<string, StoredEvent[]>>({});
  const providers = useMemo(() => (browserKey ? browserKey.split(',') : []), [browserKey]);

  useEffect(() => {
    if (!workspaceId || typeof Worker === 'undefined') return;
    const subscription = subscribeWorker(
      workspaceId,
      {
        providers,
        scope,
        active,
        /** Record provider status or schedule a store re-read; the worker never sends anything else. */
        onMessage: (data: WorkerOutbound) => {
          if (data.type === 'changed') setVersion((current) => current + 1);
          else if (data.type === 'items') setWorkerItems((current) => ({ ...current, [data.provider]: data.items }));
          else {
            // A clean run wrote to the store, so any in-memory fallback rows for the provider are obsolete.
            if (data.ok)
              setWorkerItems((current) =>
                data.provider in current ? Object.fromEntries(Object.entries(current).filter(([id]) => id !== data.provider)) : current,
              );
            setWorkerStatus((current) => ({
              ...current,
              [data.provider]: data.ok
                ? { fetchedAt: data.fetchedAt }
                : { fetchedAt: data.fetchedAt, error: liveErrorKey(data.error ?? '', data.provider) },
            }));
          }
        },
      },
      () => new Worker(new URL('./live-feed.worker.ts', import.meta.url), { type: 'module' }) as unknown as PoolWorker,
    );
    subRef.current = subscription;
    return () => {
      subscription.release();
      subRef.current = null;
      setWorkerStatus({});
      setWorkerItems({});
    };
    // Providers, scope and activity reach the shared worker through the update effect below, not by re-subscribing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  useEffect(() => {
    // scope is compared by the fields browser sources read, not by identity.
    subRef.current?.update({ providers, scope, active });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, providers, active, scope.live_map_github_owner, scope.live_map_github_repo]);
  return {
    workerStatus,
    workerItems,
    version,
    bump: () => setVersion((current) => current + 1),
    refresh: () => subRef.current?.refresh(),
  };
}

/**
 * Status of one proxied provider's query. A failed refetch keeps the previous rows (react-query retains `data`), so the
 * error carries `staleSince` (the last successful fetch) and the UI can say the shown rows are old.
 * @returns The status, or undefined while the query has neither data nor an error.
 */
export function proxiedStatus(
  result: { isError: boolean; error: unknown; data?: LiveResponse },
  provider: string,
  isOwner: boolean,
): ProviderStatus | undefined {
  if (!isOwner) return { error: 'liveMapOwnerOnly' };
  if (result.isError) return { error: liveErrorKey(result.error, provider), staleSince: result.data?.fetched_at };
  if (result.data) return { fetchedAt: result.data.fetched_at, truncated: result.data.truncated };
  return undefined;
}

/**
 * Owns the live feed of one map: a module worker fetching browser providers into IndexedDB, react-query for
 * proxied providers (also upserted into the store), and the filtered, merged read model.
 *
 * Terms gating: only providers whose terms the owner accepted are fetched or shown, on both paths. The worker
 * stays alive while this hook is mounted but polls and refetches only while `active` (gadget on screen) and the
 * tab is visible, so scrolling or switching tabs keeps filters and the map; it is terminated on unmount.
 * An already in-flight proxied request is left to finish when the gadget goes inactive. Without Worker or IndexedDB support the proxied providers still work in memory.
 *
 * @returns Filtered events (newest first), stable map points, per-provider statuses and the config query.
 */
export function useLiveFeed(scope: GadgetScope, filters: { tags: string[]; days: string; query: string; now: number }, active = true) {
  const workspace = useWorkspace();
  const workspaceId = workspace.selection?.id;
  // /live-map/events is owner-only; observers can read the config but would only collect 404s.
  const { isOwner } = workspace;
  const providers = scope.live_map_providers ?? DEFAULT_PROVIDERS;
  const config = useQuery({
    queryKey: ['live-map-config', workspaceId],
    queryFn: ({ signal }) => apiRequest<LiveConfig>('/api/v1/live-map/config', { signal }),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
    gcTime: 0,
    refetchInterval: active ? 60_000 : false,
    refetchIntervalInBackground: false,
  });
  const accepted = providers.filter((provider) => termsAccepted(config, provider));
  const browserKey = accepted.filter(isBrowserProvider).join(',');
  const proxied = providers.filter((provider) => !isBrowserProvider(provider));
  const results = useQueries({
    queries: proxied.map((provider) => ({
      queryKey: ['live-map', workspaceId, provider, scope.live_map_country, scope.live_map_sec_cik, config.data?.revision],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchLiveEvents(provider, scope, signal),
      enabled: active && isOwner && Boolean(workspaceId) && termsAccepted(config, provider),
      staleTime: 60_000,
      gcTime: 0,
      refetchInterval: active ? 60_000 : false,
      refetchIntervalInBackground: false,
      retry: false,
    })),
  });

  const { workerStatus, workerItems, version, bump, refresh: refreshWorker } = useBrowserWorker(workspaceId, browserKey, scope, active);
  const [stored, setStored] = useState<StoredEvent[]>([]);

  useEffect(() => {
    if (!workspaceId) return;
    let live = true;
    void loadEvents(workspaceId).then((rows) => {
      if (live) setStored(rows);
    });
    return () => {
      live = false;
    };
  }, [workspaceId, version]);

  const acceptedKey = accepted.join(',');
  // Identifies the query outcomes the memos below derive from (results is a fresh array every render).
  const proxyStamp = results.map((result) => `${result.dataUpdatedAt}:${result.errorUpdatedAt}`).join(',');
  const proxyEntries = useMemo(
    () =>
      proxied.flatMap((provider, index) => {
        const result = results[index];
        if (!proxyIncluded(result, termsAccepted(config, provider), isOwner)) return [];
        const seenAt = new Date(result.dataUpdatedAt || 0).toISOString();
        return (result.data?.items ?? []).map((item) => toStored(provider, providerTags(provider), item, seenAt));
      }),
    // Derived from the query results identified by proxyStamp, plus ownership and the accepted terms.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [proxyStamp, isOwner, acceptedKey, config.data],
  );
  useEffect(() => {
    // With no rows there is nothing to write; an empty batch never prunes (see staleKeys), so skipping is equivalent.
    // Once logout or erasure has started teardown, a late query must not reopen (and so recreate) the feed database.
    if (!workspaceId || !proxyEntries.length || isLiveFeedTeardownStarted()) return;
    // Best-effort persistence: an unavailable or full store must not break the live (in-memory) view.
    // Only providers whose response was included, loaded and complete may prune their vanished rows.
    const included = proxied.map((provider, index) => proxyIncluded(results[index], termsAccepted(config, provider), isOwner));
    void upsertEvents(workspaceId, proxyEntries, Date.now(), snapshotProviders(proxied, results, included)).then(bump, () => undefined);
    // proxyEntries derives from the query results identified by proxyStamp.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, proxyStamp]);

  // Search input stays responsive; the heavy filter runs on the deferred value.
  const query = useDeferredValue(filters.query);
  const events = useMemo(
    () =>
      filterEvents(
        mergeEvents(stored, Object.values(workerItems).flat(), proxyEntries),
        { tags: filters.tags, days: Number(filters.days), query, providers: accepted },
        filters.now,
      ),
    // `accepted` is identified by acceptedKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stored, workerItems, proxyEntries, filters.tags, filters.days, query, filters.now, acceptedKey],
  );
  // Keep the map's point identity stable until its serialized geometry changes so map interaction is not reset.
  const pointJson = JSON.stringify(liveMapPoints(events));
  const points = useMemo(() => JSON.parse(pointJson) as ReturnType<typeof liveMapPoints>, [pointJson]);

  const statuses: Record<string, ProviderStatus> = { ...workerStatus };
  proxied.forEach((provider, index) => {
    const status = proxiedStatus(results[index], provider, isOwner);
    if (status) statuses[provider] = status;
  });
  /** Manual refresh of both paths: refetch proxied queries and ask the worker to fetch its providers now. */
  const refresh = () => {
    for (const result of results) void result.refetch();
    refreshWorker();
  };
  return { workspaceId, isOwner, providers, accepted, config, results, events, points, statuses, refresh };
}
