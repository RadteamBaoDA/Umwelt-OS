'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useWorkspaceSession as readWorkspaceSession } from '@/core/app-shell/workspace-session';
import { workspaceGeneration } from '@/core/api';
import { workspaceKeys } from '@/core/query-keys';
import { useWorkspace as readWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, readTranslationBatch, submitTranslationBatch } from './api';
import type { ResourceType, TranslationItemRequest, TranslationResult } from './types';

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
export const MAX_POLL_MS = 60_000;

export type TranslationTarget = { id: string; revision: string };
export type ContentTranslation = {
  enabled: boolean;
  targetLanguage: 'vi' | 'en';
  /** Ready translations by resource id; anything missing renders the original. */
  results: ReadonlyMap<string, TranslationResult>;
  /** Pending, blocked and failed resources by id, for a short reason line (the original is still shown). */
  issues: ReadonlyMap<string, TranslationResult>;
  /** Resource ids currently being submitted or polled. */
  pendingIds: ReadonlySet<string>;
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

/** True when an item returned by POST /batches needs a GET /batches/{id} read to fetch its payload or error code. */
function itemNeedsHydration(item: Seen): boolean {
  if (item.status === 'ready') return !item.translation;
  if (item.status === 'blocked' || item.status === 'failed') return item.error_code === undefined;
  return false;
}

/** Polls a submitted translation batch every 2 s while the tab is visible until settled or `deadline`. */
async function pollBatchUntilSettled(params: {
  batchId: string;
  signal: AbortSignal;
  deadline: number;
  valid: () => boolean;
  take: (items: Seen[]) => void;
}): Promise<void> {
  const { batchId, signal, deadline, valid, take } = params;
  let pending = true;
  while (pending && valid() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    if (!valid() || Date.now() >= deadline) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') continue;
    const batch = await readTranslationBatch(batchId, signal);
    if (!valid()) return;
    take(batch.items);
    pending = batch.items.some((i) => i.status === 'pending');
  }
}

/** Submits one chunk of at most 25 targets, immediately hydrates cache hits, and polls pending items until `deadline`. */
async function runTranslationGroup(params: {
  type: ResourceType;
  group: TranslationTarget[];
  csrfToken: string;
  signal: AbortSignal;
  deadline: number;
  valid: () => boolean;
  take: (items: Seen[]) => void;
}): Promise<void> {
  const { type, group, csrfToken, signal, deadline, valid, take } = params;
  const requests: TranslationItemRequest[] = group.map((t) => ({
    resource_type: type,
    resource_id: t.id,
    resource_revision: t.revision,
  }));
  const accepted = await submitTranslationBatch(requests, csrfToken, signal);
  if (!valid()) return;
  const seenAccepted = accepted.items as Seen[];
  const needsHydration = Boolean(accepted.batch_id) && seenAccepted.some(itemNeedsHydration);
  take(needsHydration ? seenAccepted.filter((item) => item.status === 'pending' || item.status === 'unchanged') : seenAccepted);

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

/** Deduplicates requested targets, skipping items already resolved in cache or currently in flight. */
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
 * a payload are hydrated immediately via GET /batches/{id}; pending items poll every 2 s while visible
 * for at most 60 s. Everything in flight is aborted on unmount, workspace switch, or settings change.
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
      };

      // Errors are swallowed on purpose: the original stays visible.
      void Promise.all(
        chunk(todo).map((group) =>
          runTranslationGroup({ type, group, csrfToken, signal: controller.signal, deadline, valid, take }).catch(() => undefined),
        ),
      ).finally(() => {
        controllersRef.current.delete(controller);
        for (const item of todo) inFlightIdsRef.current.delete(item.id);
        setSubmittingIds(new Set(inFlightIdsRef.current));
      });
    },
    [enabled, workspaceId, targets, queryClient, keyOf, type, csrfToken],
  );

  // Cheap to derive every render; entries are observers of the shared cache.
  const results = new Map<string, TranslationResult>();
  const issues = new Map<string, TranslationResult>();
  const pendingIds = new Set<string>(submittingIds);
  if (enabled) {
    targets.forEach((t, i) => {
      const data = entries[i]?.data as TranslationResult | undefined;
      if (!data) return;
      if (data.status === 'ready' && data.translation) results.set(t.id, data);
      else if (data.status === 'pending' || data.status === 'blocked' || data.status === 'failed') {
        issues.set(t.id, data);
        if (data.status === 'pending') pendingIds.add(t.id);
      }
    });
  }
  return { enabled, targetLanguage, results, issues, pendingIds, translate };
}
