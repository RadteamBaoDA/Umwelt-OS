'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
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
  | 'liveMapSourceError';

type LiveResponse = { items: LiveEvent[]; fetched_at: string; truncated: boolean };

/** True when the config loaded without error and the owner accepted the provider's terms. */
export function termsAccepted(config: { isError: boolean; data?: LiveConfig }, id: string): boolean {
  return !config.isError && Boolean(config.data?.terms_catalog.some((term) => term.id === id && term.accepted));
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
 * visibility (no background polling) and terminate on unmount.
 * @returns Worker-reported statuses, a counter that increments whenever the worker wrote new rows, `bump` to force a store re-read, and `refresh` to ask the worker to fetch now.
 */
function useBrowserWorker(workspaceId: string | undefined, browserKey: string, scope: GadgetScope) {
  const workerRef = useRef<Worker | null>(null);
  const [workerStatus, setWorkerStatus] = useState<Record<string, ProviderStatus>>({});
  const [version, setVersion] = useState(0);

  useEffect(() => {
    if (!workspaceId || typeof Worker === 'undefined') return;
    const next = new Worker(new URL('./live-feed.worker.ts', import.meta.url), { type: 'module' });
    /** Record provider status or schedule a store re-read; the worker never sends anything else. */
    next.onmessage = (message: MessageEvent<WorkerOutbound>) => {
      const data = message.data;
      if (data.type === 'changed') setVersion((current) => current + 1);
      else
        setWorkerStatus((current) => ({
          ...current,
          [data.provider]: data.ok
            ? { fetchedAt: data.fetchedAt }
            : { fetchedAt: data.fetchedAt, error: liveErrorKey(data.error ?? '', data.provider) },
        }));
    };
    workerRef.current = next;
    return () => {
      next.terminate();
      workerRef.current = null;
      setWorkerStatus({});
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
    /** Poll only while the tab is visible. */
    const update = () => worker.postMessage({ type: 'visibility', visible: document.visibilityState === 'visible' });
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, [workspaceId]);
  return {
    workerStatus,
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
 * runs only while this hook is mounted (the gadget mounts it while visible) and pauses when the tab is hidden;
 * it is terminated on unmount. Without Worker or IndexedDB support the proxied providers still work in memory.
 *
 * @returns Filtered events (newest first), stable map points, per-provider statuses and the config query.
 */
export function useLiveFeed(scope: GadgetScope, filters: { tags: string[]; days: string; query: string; now: number }) {
  const workspace = useWorkspace();
  const workspaceId = workspace.selection?.id;
  const providers = scope.live_map_providers ?? DEFAULT_PROVIDERS;
  const config = useQuery({
    queryKey: ['live-map-config', workspaceId],
    queryFn: ({ signal }) => apiRequest<LiveConfig>('/api/v1/live-map/config', { signal }),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
    gcTime: 0,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
  const accepted = providers.filter((provider) => termsAccepted(config, provider));
  const browserKey = accepted.filter(isBrowserProvider).join(',');
  const proxied = providers.filter((provider) => !isBrowserProvider(provider));
  const results = useQueries({
    queries: proxied.map((provider) => ({
      queryKey: ['live-map', workspaceId, provider, scope.live_map_country, scope.live_map_sec_cik, config.data?.revision],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchLiveEvents(provider, scope, signal),
      enabled: Boolean(workspaceId) && termsAccepted(config, provider),
      staleTime: 60_000,
      gcTime: 0,
      refetchInterval: 60_000,
      refetchIntervalInBackground: false,
      retry: false,
    })),
  });

  const { workerStatus, version, bump, refresh: refreshWorker } = useBrowserWorker(workspaceId, browserKey, scope);
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

  const proxyEntries = proxied.flatMap((provider, index) => {
    const result = results[index];
    if (!termsAccepted(config, provider) || (result.error instanceof ApiError && result.error.status === 409)) return [];
    const seenAt = new Date(result.dataUpdatedAt || 0).toISOString();
    return (result.data?.items ?? []).map((item) => toStored(provider, providerTags(provider), item, seenAt));
  });
  const proxyStamp = results.map((result) => result.dataUpdatedAt).join(',');
  useEffect(() => {
    if (!workspaceId || !proxyEntries.length) return;
    // Best-effort persistence: an unavailable or full store must not break the live (in-memory) view.
    void upsertEvents(workspaceId, proxyEntries).then(bump, () => undefined);
    // proxyEntries derives from the query results identified by proxyStamp.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, proxyStamp]);

  // Fresh proxy rows win over the stored copy of the same key.
  const merged = new Map<string, StoredEvent>();
  for (const row of [...stored, ...proxyEntries]) merged.set(row.key, row);
  const events = filterEvents(
    [...merged.values()],
    { tags: filters.tags, days: Number(filters.days), query: filters.query, providers: accepted },
    filters.now,
  );
  // Keep the map's point identity stable until its serialized geometry changes so map interaction is not reset.
  const pointJson = JSON.stringify(liveMapPoints(events));
  const points = useMemo(() => JSON.parse(pointJson) as ReturnType<typeof liveMapPoints>, [pointJson]);

  const statuses: Record<string, ProviderStatus> = { ...workerStatus };
  proxied.forEach((provider, index) => {
    const result = results[index];
    if (result.isError) statuses[provider] = { error: liveErrorKey(result.error, provider) };
    else if (result.data) statuses[provider] = { fetchedAt: result.data.fetched_at, truncated: result.data.truncated };
  });
  /** Manual refresh of both paths: refetch proxied queries and ask the worker to fetch its providers now. */
  const refresh = () => {
    for (const result of results) void result.refetch();
    refreshWorker();
  };
  return { workspaceId, providers, config, results, events, points, statuses, refresh };
}
