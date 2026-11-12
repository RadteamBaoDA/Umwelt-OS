'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiRequest } from '@/core/api';
import { useWorkspace } from '@/core/workspace-context';
import type { GadgetScope } from '../api';
import { liveMapPoints, type LiveEvent } from '../live-map-data';
import type { WorkerInbound, WorkerOutbound } from './runner';
import { isBrowserProvider, providerTags } from './sources';
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

/** Live-map config response: revision plus the provider terms catalog (accepted flag and attribution). */
export type LiveConfig = { revision: number; terms_catalog: { id: string; attribution: string; accepted: boolean }[] };

/** Last known fetch state of one provider, from either path; `error` is already a translation key. */
export type ProviderStatus = { fetchedAt?: string; error?: LiveErrorKey; truncated?: boolean };

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

type LiveResponse = { items: LiveEvent[]; fetched_at: string; truncated: boolean };

/** True when the config loaded without error and the owner accepted the provider's terms. */
export function termsAccepted(config: { isError: boolean; data?: LiveConfig }, id: string): boolean {
  return !config.isError && Boolean(config.data?.terms_catalog.some((term) => term.id === id && term.accepted));
}

/**
 * Proxied providers whose response is a complete snapshot: loaded and not truncated. A truncated response omits rows
 * that still exist upstream, so deleting the stored rows it lacks would drop live events.
 */
export function snapshotProviders(proxied: readonly string[], results: readonly { data?: { truncated?: boolean } }[]): string[] {
  return proxied.filter((_, index) => results[index]?.data && !results[index].data?.truncated);
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
 * Owns the module worker lifecycle: spawn per workspace, post the accepted browser providers and scope, mirror tab
 * and gadget visibility (`active`; no background polling while hidden) and terminate on unmount.
 * @returns Worker-reported statuses, in-memory rows for providers whose store write failed, a counter that increments whenever the worker wrote new rows, `bump` to force a store re-read, and `refresh` to ask the worker to fetch now.
 */
function useBrowserWorker(workspaceId: string | undefined, browserKey: string, scope: GadgetScope, active: boolean) {
  const workerRef = useRef<Worker | null>(null);
  const [workerStatus, setWorkerStatus] = useState<Record<string, ProviderStatus>>({});
  const [version, setVersion] = useState(0);
  // Rows the worker could not persist, kept in memory per provider until a later write succeeds.
  const [workerItems, setWorkerItems] = useState<Record<string, StoredEvent[]>>({});

  useEffect(() => {
    if (!workspaceId || typeof Worker === 'undefined') return;
    const next = new Worker(new URL('./live-feed.worker.ts', import.meta.url), { type: 'module' });
    /** Record provider status or schedule a store re-read; the worker never sends anything else. */
    next.onmessage = (message: MessageEvent<WorkerOutbound>) => {
      const data = message.data;
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
    };
    workerRef.current = next;
    return () => {
      next.terminate();
      workerRef.current = null;
      setWorkerStatus({});
      setWorkerItems({});
    };
  }, [workspaceId]);

  useEffect(() => {
    const worker = workerRef.current;
    if (worker && workspaceId)
      worker.postMessage({ type: 'config', providers: browserKey ? browserKey.split(',') : [], scope, workspaceId });
    // scope is compared by the fields browser sources read, not by identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, browserKey, scope.live_map_github_owner, scope.live_map_github_repo]);

  useEffect(() => {
    const worker = workerRef.current;
    if (!worker) return;
    /** Poll only while the tab is visible and the owning gadget is on screen. */
    const update = () => worker.postMessage({ type: 'visibility', visible: active && document.visibilityState === 'visible' });
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, [workspaceId, active]);
  return {
    workerStatus,
    workerItems,
    version,
    bump: () => setVersion((current) => current + 1),
    refresh: () => workerRef.current?.postMessage({ type: 'refresh' } satisfies WorkerInbound),
  };
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
        if (!isOwner || !termsAccepted(config, provider) || (result.error instanceof ApiError && result.error.status === 409)) return [];
        const seenAt = new Date(result.dataUpdatedAt || 0).toISOString();
        return (result.data?.items ?? []).map((item) => toStored(provider, providerTags(provider), item, seenAt));
      }),
    // Derived from the query results identified by proxyStamp, plus ownership and the accepted terms.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [proxyStamp, isOwner, acceptedKey, config.data],
  );
  useEffect(() => {
    if (!workspaceId || !proxyEntries.length) return;
    // Best-effort persistence: an unavailable or full store must not break the live (in-memory) view.
    // Providers with a complete (non-truncated) response are full snapshots, so their vanished rows are replaced.
    void upsertEvents(workspaceId, proxyEntries, Date.now(), snapshotProviders(proxied, results)).then(bump, () => undefined);
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
    const result = results[index];
    if (!isOwner) statuses[provider] = { error: 'liveMapOwnerOnly' };
    else if (result.isError) statuses[provider] = { error: liveErrorKey(result.error, provider) };
    else if (result.data) statuses[provider] = { fetchedAt: result.data.fetched_at, truncated: result.data.truncated };
  });
  /** Manual refresh of both paths: refetch proxied queries and ask the worker to fetch its providers now. */
  const refresh = () => {
    for (const result of results) void result.refetch();
    refreshWorker();
  };
  return { workspaceId, isOwner, providers, accepted, config, results, events, points, statuses, refresh };
}
