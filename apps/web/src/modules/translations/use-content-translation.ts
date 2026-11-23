'use client';

import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { useQueries, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query';
import { useWorkspaceSession as readWorkspaceSession } from '@/core/app-shell/workspace-session';
import { ApiError, workspaceGeneration } from '@/core/api';
import { workspaceKeys } from '@/core/query-keys';
import { useWorkspace as readWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, readTranslationBatch, submitTranslationBatch } from './api';
import type { LiveEventSource, ResourceType, TranslationItemRequest, TranslationResult, TranslationSettings } from './types';

/** Reason code stored on a batch that is still pending at the client deadline; the item stays retryable. */
export const STILL_QUEUED_CODE = 'still_queued';

/** Reads the workspace session (calling its single `useContext` hook), returning null when rendered outside a provider in unit tests. */
function readSafeWorkspaceSession(): { csrfToken: string } | null {
  try {
    return readWorkspaceSession();
  } catch {
    return null;
  }
}

/** Reads the workspace context (calling its single `useContext` hook), returning null when rendered outside a provider in unit tests. */
function readSafeWorkspace(): { selection?: { id: string } | null } | null {
  try {
    return readWorkspace();
  } catch {
    return null;
  }
}

export const CHUNK_SIZE = 25;
export const POLL_MS = 2000;
/** After this long the item is still queued behind other model work: keep waiting, but poll less often. */
export const FAST_POLL_WINDOW_MS = 60_000;
export const SLOW_POLL_MS = 10_000;
/** Hard client deadline; past it the item becomes `still_queued` and the button is clickable again. */
export const MAX_POLL_MS = 10 * 60_000;

/** One translatable resource; `source` is the inline text snapshot required only for `live_event`. */
export type TranslationTarget = { id: string; revision: string; source?: LiveEventSource };
export type ContentTranslation = {
  enabled: boolean;
  targetLanguage: 'vi' | 'en';
  /** Ready translations by resource id; anything missing renders the original. */
  results: ReadonlyMap<string, TranslationResult>;
  /** Pending, blocked and failed resources by id, for a short reason line (the original is still shown). */
  issues: ReadonlyMap<string, TranslationResult>;
  /** Resource ids currently being submitted or polled. */
  pendingIds: ReadonlySet<string>;
  /** Resource ids the server reported as already in the target language: render a note, not a Translate button. */
  unchanged: ReadonlySet<string>;
  /** Requests on-demand translation for the given subset (or all current `targets` when omitted). */
  translate: (subset?: TranslationTarget[]) => void;
};

/** Cache key of one translated resource revision: [workspace, generation, type, id, revision, settings revision]. */
export function translationKey(
  workspaceId: string | null,
  generation: number,
  type: ResourceType,
  id: string,
  revision: string,
  settingsRevision: number,
) {
  return workspaceKeys.translation(workspaceId, generation, type, id, revision, settingsRevision);
}

/** Splits values into groups of at most `size` (the batch API accepts 25). */
export function chunk<T>(values: T[], size = CHUNK_SIZE): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < values.length; i += size) out.push(values.slice(i, i + size));
  return out;
}

type Seen = {
  resource_id: string;
  status: TranslationResult['status'];
  translation?: TranslationResult['translation'];
  original_revision?: string;
  error_code?: string | null;
};

/** True when a cached entry already carries its final payload/status and needs no re-submission. */
function isResolvedCache(cached: TranslationResult | undefined): boolean {
  if (!cached) return false;
  return cached.status === 'unchanged' || (cached.status === 'ready' && cached.translation !== null);
}

/**
 * True when an item returned by POST /batches needs a GET /batches/{id} read to fetch its payload or error code.
 * Ready items need a payload; blocked/failed items need an error code. An invalid item rejected by the batch
 * endpoint arrives already carrying `error_code` (resource_unavailable / stale_request), so it is settled from
 * the POST response and never re-read. A missing or null code marks a cache hit whose reason only GET returns.
 *
 * @param item One item from the POST /batches response.
 * @returns True only when the item must be re-read before its outcome is known.
 */
function itemNeedsHydration(item: Seen): boolean {
  if (item.status === 'ready') return !item.translation;
  if (item.status === 'blocked' || item.status === 'failed') return item.error_code == null;
  return false;
}

/**
 * Polls a submitted translation batch while the tab is visible until settled or `deadline`: every 2 s for the
 * first minute, then every 10 s. A slow model queue (translation shares workers with extraction) can take minutes,
 * and the result should still land in the row without the user clicking again.
 */
async function pollBatchUntilSettled(params: {
  batchId: string;
  signal: AbortSignal;
  deadline: number;
  valid: () => boolean;
  take: (items: Seen[]) => void;
}): Promise<void> {
  const { batchId, signal, deadline, valid, take } = params;
  const started = Date.now();
  let pending = true;
  while (pending && valid() && Date.now() < deadline) {
    const interval = Date.now() - started < FAST_POLL_WINDOW_MS ? POLL_MS : SLOW_POLL_MS;
    await new Promise((resolve) => setTimeout(resolve, interval));
    if (!valid() || Date.now() >= deadline) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') continue;
    const batch = await readTranslationBatch(batchId, signal);
    if (!valid()) return;
    take(batch.items);
    pending = batch.items.some((i) => i.status === 'pending');
  }
}

/**
 * Submits one chunk of at most 25 targets, immediately hydrates cache hits, and polls pending items until `deadline`.
 * On a successful POST the server's current settings are pushed into the settings cache so a stale toggle
 * or language in the header is corrected as soon as any translation request is accepted.
 */
async function runTranslationGroup(params: {
  type: ResourceType;
  group: TranslationTarget[];
  csrfToken: string;
  signal: AbortSignal;
  deadline: number;
  valid: () => boolean;
  take: (items: Seen[]) => void;
  onSettings: (settings: TranslationSettings) => void;
}): Promise<void> {
  const { type, group, csrfToken, signal, deadline, valid, take, onSettings } = params;
  const requests: TranslationItemRequest[] = group.map((t) => ({
    resource_type: type,
    resource_id: t.id,
    resource_revision: t.revision,
    ...(t.source ? { source: t.source } : {}),
  }));
  const accepted = await submitTranslationBatch(requests, csrfToken, signal);
  if (!valid()) return;
  onSettings(accepted.settings);
  const seenAccepted = accepted.items as Seen[];
  const needsHydration = Boolean(accepted.batch_id) && seenAccepted.some(itemNeedsHydration);
  // Settled items (including rejected ones with an error_code) are taken now; only items still lacking their
  // payload or error code wait for the GET below, so an invalid item is never dropped from the batch.
  take(needsHydration ? seenAccepted.filter((item) => !itemNeedsHydration(item)) : seenAccepted);

  let pending = seenAccepted.some((i) => i.status === 'pending');
  // Cache hits (ready/blocked/failed) from POST /batches carry no payload or error_code: hydrate immediately.
  if (accepted.batch_id && needsHydration) {
    const batch = await readTranslationBatch(accepted.batch_id, signal);
    if (!valid()) return;
    take(batch.items);
    pending = batch.items.some((i) => i.status === 'pending');
  }
  if (accepted.batch_id && pending) {
    await pollBatchUntilSettled({ batchId: accepted.batch_id, signal, deadline, valid, take });
  }
}

/**
 * Maps a failed submit or poll to the reason code shown in the UI. Only ApiError carries an HTTP status;
 * any other error (network failure, parse error) is a generic request failure.
 *
 * @param error The rejection reason caught from `runTranslationGroup`.
 * @returns `rate_limited` (429), `stale_request` (409), `resource_unavailable` (404) or `request_failed`.
 */
export function requestFailureCode(error: unknown): string {
  if (!(error instanceof ApiError)) return 'request_failed';
  if (error.status === 429) return 'rate_limited';
  if (error.status === 409) return 'stale_request';
  if (error.status === 404) return 'resource_unavailable';
  return 'request_failed';
}

/**
 * Marks every target of a failed group as `failed` with `code`. Entries that already hold a settled outcome
 * (ready, unchanged, blocked, failed) are kept, so a poll error after partial success does not overwrite
 * translations that already arrived.
 *
 * @param queryClient Shared react-query client holding the translation cache.
 * @param group Targets submitted in the failed chunk.
 * @param keyFor Builds the cache key of one target for the run's workspace generation.
 * @param code Reason code to store in `errorCode`.
 */
function failGroup(
  queryClient: QueryClient,
  group: TranslationTarget[],
  keyFor: (item: TranslationTarget) => QueryKey,
  code: string,
): void {
  for (const item of group) {
    const cached = queryClient.getQueryData<TranslationResult>(keyFor(item));
    if (cached && cached.status !== 'pending') continue;
    queryClient.setQueryData<TranslationResult>(keyFor(item), { status: 'failed', translation: null, errorCode: code });
  }
}

/**
 * Settles cache entries still reading `pending` after a run ends. Without this, the entry (staleTime Infinity)
 * would keep the button disabled until a reload.
 * - Deadline reached while the run was still live (`stillQueued` true): the entry becomes a `failed` issue with
 *   `still_queued`, so the note explains the wait and the Translate button is clickable again (a retry
 *   re-submits and the server returns the existing row).
 * - Run aborted (unmount, workspace switch, settings change): the entry is removed silently, as before.
 *
 * @param queryClient Shared react-query client holding the translation cache.
 * @param todo Targets this run was responsible for.
 * @param keyFor Builds the cache key of one target for the run's workspace generation.
 * @param stillQueued True only when the run ended by reaching its deadline without being aborted.
 */
function settleStalePending(
  queryClient: QueryClient,
  todo: TranslationTarget[],
  keyFor: (item: TranslationTarget) => QueryKey,
  stillQueued: boolean,
): void {
  for (const item of todo) {
    const key = keyFor(item);
    if (queryClient.getQueryData<TranslationResult>(key)?.status !== 'pending') continue;
    if (stillQueued)
      queryClient.setQueryData<TranslationResult>(key, { status: 'failed', translation: null, errorCode: STILL_QUEUED_CODE });
    else queryClient.removeQueries({ queryKey: key, exact: true });
  }
}

/**
 * Deduplicates requested targets, skipping items already resolved in cache or currently in flight.
 *
 * @param requested Targets asked for by the caller.
 * @param inFlight Resource ids a live run is already submitting or polling.
 * @param getCached Reads the cached outcome of one target.
 * @returns Unique targets that still need a submission.
 */
function selectTodoTargets(
  requested: TranslationTarget[],
  inFlight: ReadonlySet<string>,
  getCached: (item: TranslationTarget) => TranslationResult | undefined,
): TranslationTarget[] {
  const unique = new Map<string, TranslationTarget>();
  for (const item of requested) {
    if (!item.id || !item.revision || inFlight.has(item.id) || isResolvedCache(getCached(item))) continue;
    unique.set(item.id, item);
  }
  return [...unique.values()];
}

/**
 * On-demand content translation for the given resources. Nothing is submitted on mount: callers invoke
 * `translate()` (for all targets) or `translate([target])` (for one item) when the user explicitly clicks
 * a Translate button. Results live in the shared react-query cache (one entry per resource revision), so
 * list cards and detail dialogs share them immediately. Cached DB hits returned by POST /batches without
 * a payload are hydrated immediately via GET /batches/{id}; pending items poll while visible, every 2 s for the
 * first minute and every 10 s after, for at most 10 min. Everything in flight is aborted on unmount, workspace
 * switch, or settings change. Request failures (429, 409, 404, others) are stored per target as `failed` with a
 * reason code. An entry still `pending` at the 10 min deadline becomes `failed` with `still_queued`; an entry left
 * `pending` by an aborted run
 * is removed. Both leave the Translate button clickable.
 *
 * @param type Resource type supported by the translation API.
 * @param targets Currently loaded resource references (id and revision).
 * @returns Workspace translation state, cached ready results, issues, pending ids, and the on-demand trigger.
 */
export function useContentTranslation(type: ResourceType, targets: TranslationTarget[]): ContentTranslation {
  const session = readSafeWorkspaceSession();
  const csrfToken = session?.csrfToken ?? '';
  const workspace = readSafeWorkspace();
  const queryClient = useQueryClient();
  const workspaceId = workspace?.selection?.id ?? null;
  const settings = useQuery({
    queryKey: ['translation-settings', workspaceId],
    queryFn: ({ signal }) => fetchTranslationSettings(signal),
    staleTime: 30_000,
    enabled: Boolean(workspaceId && csrfToken),
  });
  const enabled = settings.data?.enabled === true;
  const settingsRevision = settings.data?.configuration_revision ?? 0;
  const targetLanguage = settings.data?.target_language ?? 'vi';
  const scopeKey = `${workspaceId}|${type}|${settingsRevision}|${targetLanguage}`;

  const keyOf = useCallback(
    (t: TranslationTarget, generation: number) => translationKey(workspaceId, generation, type, t.id, t.revision, settingsRevision),
    [workspaceId, type, settingsRevision],
  );

  // Observers only: `translate()` populates the shared cache in batches of at most 25.
  const generationNow = workspaceGeneration();
  const entries = useQueries({
    queries: targets.map((t) => ({
      queryKey: keyOf(t, generationNow),
      queryFn: () => Promise.reject(new Error('filled by useContentTranslation')),
      enabled: false,
      staleTime: Infinity,
      retry: false,
    })),
  });

  const controllersRef = useRef<Set<AbortController>>(new Set());
  const inFlightIdsRef = useRef<Set<string>>(new Set());
  const [submittingIds, setSubmittingIds] = useState<ReadonlySet<string>>(() => new Set());
  // The disabled observer queries above do not reliably re-render this hook when a poll writes the cache mid-run
  // (seen in tests: a ready item stayed invisible until the whole run settled). Each cache write bumps this counter so
  // results appear as each poll lands, not only when the run ends.
  const [, bumpRender] = useReducer((n: number) => n + 1, 0);

  // Abort every in-flight batch when the workspace, resource type, or settings revision changes (or on unmount).
  useEffect(() => {
    const controllers = controllersRef.current;
    const inFlight = inFlightIdsRef.current;
    return () => {
      for (const controller of controllers) controller.abort();
      controllers.clear();
      inFlight.clear();
      setSubmittingIds(new Set());
    };
  }, [scopeKey, enabled]);

  const translate = useCallback(
    (subset?: TranslationTarget[]) => {
      if (!enabled || !workspaceId) return;
      const generation = workspaceGeneration();
      const todo = selectTodoTargets(subset ?? targets, inFlightIdsRef.current, (item) =>
        queryClient.getQueryData<TranslationResult>(keyOf(item, generation)),
      );
      if (todo.length === 0) return;

      const controller = new AbortController();
      controllersRef.current.add(controller);
      for (const item of todo) inFlightIdsRef.current.add(item.id);
      setSubmittingIds(new Set(inFlightIdsRef.current));

      const expected = new Map(todo.map((t) => [t.id, t.revision]));
      const valid = () => !controller.signal.aborted && workspaceGeneration() === generation;
      const deadline = Date.now() + MAX_POLL_MS;
      const keyFor = (item: TranslationTarget) => keyOf(item, generation);
      const take = (items: Seen[]) => {
        for (const item of items) {
          const revision = expected.get(item.resource_id);
          if (revision === undefined) continue;
          if (item.original_revision !== undefined && item.original_revision !== revision) continue;
          queryClient.setQueryData<TranslationResult>(keyOf({ id: item.resource_id, revision }, generation), {
            status: item.status,
            translation: item.status === 'ready' ? (item.translation ?? null) : null,
            errorCode: item.error_code ?? null,
          });
        }
        if (valid()) bumpRender();
      };
      const onSettings = (next: TranslationSettings) => queryClient.setQueryData(['translation-settings', workspaceId], next);

      // Aborts are silent (valid() is false); any other failure becomes a per-target reason instead of a silent gap.
      void Promise.all(
        chunk(todo).map((group) =>
          runTranslationGroup({ type, group, csrfToken, signal: controller.signal, deadline, valid, take, onSettings }).catch(
            (error: unknown) => {
              if (valid()) failGroup(queryClient, group, keyFor, requestFailureCode(error));
            },
          ),
        ),
      ).finally(() => {
        // Reaching the deadline keeps the item retryable with a note; an aborted run is cleaned up silently.
        settleStalePending(queryClient, todo, keyFor, valid());
        controllersRef.current.delete(controller);
        for (const item of todo) inFlightIdsRef.current.delete(item.id);
        setSubmittingIds(new Set(inFlightIdsRef.current));
      });
    },
    [enabled, workspaceId, targets, queryClient, keyOf, type, csrfToken],
  );

  return { enabled, targetLanguage, ...deriveTranslationState({ targets, entries, enabled, submittingIds }), translate };
}

/**
 * Derives the per-target display state from the cache observers on every render (cheap, no side effects).
 * A cached `pending` entry counts toward `pendingIds` only while a live run is submitting that id, so an
 * orphaned pending entry cannot disable a Translate button.
 *
 * @param params.targets Currently loaded resource references, in display order.
 * @param params.entries One react-query observer result per target, in the same order as `targets`.
 * @param params.enabled Whether workspace translation is on; when false, every map is empty.
 * @param params.submittingIds Ids of targets with a live submit or poll run.
 * @returns Ready results, issue states, unchanged ids and pending ids.
 */
function deriveTranslationState(params: {
  targets: TranslationTarget[];
  entries: ReadonlyArray<{ data?: unknown }>;
  enabled: boolean;
  submittingIds: ReadonlySet<string>;
}): Pick<ContentTranslation, 'results' | 'issues' | 'pendingIds' | 'unchanged'> {
  const { targets, entries, enabled, submittingIds } = params;
  const results = new Map<string, TranslationResult>();
  const issues = new Map<string, TranslationResult>();
  const unchanged = new Set<string>();
  const pendingIds = new Set<string>(submittingIds);
  if (enabled) {
    targets.forEach((t, i) => {
      const data = entries[i]?.data as TranslationResult | undefined;
      if (!data) return;
      if (data.status === 'unchanged') unchanged.add(t.id);
      if (data.status === 'ready' && data.translation) results.set(t.id, data);
      else if (data.status === 'pending' || data.status === 'blocked' || data.status === 'failed') {
        issues.set(t.id, data);
        if (data.status === 'pending' && submittingIds.has(t.id)) pendingIds.add(t.id);
      }
    });
  }
  return { results, issues, pendingIds, unchanged };
}
