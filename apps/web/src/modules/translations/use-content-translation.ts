'use client';

import { useEffect } from 'react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { workspaceGeneration } from '@/core/api';
import { workspaceKeys } from '@/core/query-keys';
import { useWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, readTranslationBatch, submitTranslationBatch } from './api';
import type { ResourceType, TranslationItemRequest, TranslationResult } from './types';

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
};

/** Cache key of one translated resource revision: [workspace, generation, type, id, revision, settings revision]. */
export function translationKey(workspaceId: string | null, generation: number, type: ResourceType, id: string, revision: string, settingsRevision: number) {
  return workspaceKeys.translation(workspaceId, generation, type, id, revision, settingsRevision);
}

/** Splits values into groups of at most `size` (the batch API accepts 25). */
export function chunk<T>(values: T[], size = CHUNK_SIZE): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < values.length; i += size) out.push(values.slice(i, i + size));
  return out;
}

type Seen = { resource_id: string; status: TranslationResult['status']; translation?: TranslationResult['translation']; original_revision?: string; error_code?: string | null };

/**
 * Translates only the given (currently loaded) resources. Results live in the shared react-query
 * cache (one entry per resource revision), so the Brief widget and the expanded view share them. The
 * original is always rendered first; results arrive by polling every 2 s while visible for at most 60 s
 * per mount. A request still pending afterwards stays pending: no failure, no re-submit. Everything is
 * aborted and dropped on workspace switch, unmount, settings change or a changed target list.
 */
export function useContentTranslation(type: ResourceType, targets: TranslationTarget[]): ContentTranslation {
  const { csrfToken } = useWorkspaceSession();
  const { selection } = useWorkspace();
  const queryClient = useQueryClient();
  const workspaceId = selection?.id ?? null;
  const settings = useQuery({
    queryKey: ['translation-settings', workspaceId], queryFn: ({ signal }) => fetchTranslationSettings(signal), staleTime: 30_000,
  });
  const enabled = settings.data?.enabled === true;
  const settingsRevision = settings.data?.configuration_revision ?? 0;
  const targetLanguage = settings.data?.target_language ?? 'vi';
  const signature = targets.map((t) => `${t.id}@${t.revision}`).join('|');
  const scopeKey = `${workspaceId}|${type}|${settingsRevision}|${targetLanguage}|${signature}`;
  const keyOf = (t: TranslationTarget, generation: number) => translationKey(workspaceId, generation, type, t.id, t.revision, settingsRevision);

  // Observers only: the effect below fills the cache in batches of 25, never one request per query.
  const generationNow = workspaceGeneration();
  const entries = useQueries({
    queries: targets.map((t) => ({
      queryKey: keyOf(t, generationNow), queryFn: () => Promise.reject(new Error('filled by useContentTranslation')),
      enabled: false, staleTime: Infinity, retry: false,
    })),
  });

  useEffect(() => {
    if (!enabled || !workspaceId || targets.length === 0) return;
    const controller = new AbortController();
    const generation = workspaceGeneration();
    const expected = new Map(targets.map((t) => [t.id, t.revision]));
    // A late result is applied only while workspace generation, scope and revision still match.
    const valid = () => !controller.signal.aborted && workspaceGeneration() === generation;
    const deadline = Date.now() + MAX_POLL_MS;
    const take = (items: Seen[]) => {
      for (const item of items) {
        const revision = expected.get(item.resource_id);
        if (revision === undefined) continue;
        if (item.original_revision !== undefined && item.original_revision !== revision) continue;
        const result: TranslationResult = {
          status: item.status, translation: item.status === 'ready' ? item.translation ?? null : null, errorCode: item.error_code ?? null,
        };
        queryClient.setQueryData(keyOf({ id: item.resource_id, revision }, generation), result);
      }
    };
    // Revisions already resolved (ready/unchanged/blocked/failed) are served from the shared cache.
    const todo = targets.filter((t) => {
      const cached = queryClient.getQueryData<TranslationResult>(keyOf(t, generation));
      return !cached || cached.status === 'pending';
    });

    const run = async (group: TranslationTarget[]) => {
      const requests: TranslationItemRequest[] = group.map((t) => ({ resource_type: type, resource_id: t.id, resource_revision: t.revision }));
      const accepted = await submitTranslationBatch(requests, csrfToken, controller.signal);
      if (!valid()) return;
      take(accepted.items);
      let pending = accepted.items.some((i) => i.status === 'pending');
      while (accepted.batch_id && pending && valid() && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (!valid() || Date.now() >= deadline) return;
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') continue;
        const batch = await readTranslationBatch(accepted.batch_id, controller.signal);
        if (!valid()) return;
        take(batch.items);
        pending = batch.items.some((i) => i.status === 'pending');
      }
    };
    // Errors are swallowed on purpose: the original stays visible.
    void Promise.all(chunk(todo).map((group) => run(group).catch(() => undefined)));
    return () => controller.abort();
    // `targets` is represented by `signature` inside scopeKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, scopeKey, csrfToken]);

  // Cheap to derive every render; entries are observers of the shared cache.
  const results = new Map<string, TranslationResult>();
  const issues = new Map<string, TranslationResult>();
  if (enabled) {
    targets.forEach((t, i) => {
      const data = entries[i]?.data as TranslationResult | undefined;
      if (!data) return;
      if (data.status === 'ready' && data.translation) results.set(t.id, data);
      else if (data.status === 'pending' || data.status === 'blocked' || data.status === 'failed') issues.set(t.id, data);
    });
  }
  return { enabled, targetLanguage, results, issues };
}
