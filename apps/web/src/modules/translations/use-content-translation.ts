'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { workspaceGeneration } from '@/core/api';
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
};
const EMPTY: ReadonlyMap<string, TranslationResult> = new Map();

/** Cache key of one translated resource revision: [workspace, generation, type, id, revision, settings revision]. */
export function translationKey(workspaceId: string | null, generation: number, type: ResourceType, id: string, revision: string, settingsRevision: number) {
  return [workspaceId, generation, type, id, revision, settingsRevision] as const;
}

/** Splits values into groups of at most `size` (the batch API accepts 25). */
export function chunk<T>(values: T[], size = CHUNK_SIZE): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < values.length; i += size) out.push(values.slice(i, i + size));
  return out;
}

type Seen = { resource_id: string; status: TranslationResult['status']; translation?: TranslationResult['translation']; original_revision?: string };

/**
 * Translates only the given (currently loaded) resources. The original is always rendered first;
 * results arrive by polling every 2 s while visible for at most 60 s per mount. A request still
 * pending afterwards stays pending: no failure, no re-submit. Everything is aborted and dropped on
 * workspace switch, unmount, settings change or a changed target list.
 */
export function useContentTranslation(type: ResourceType, targets: TranslationTarget[]): ContentTranslation {
  const { csrfToken } = useWorkspaceSession();
  const { selection } = useWorkspace();
  const workspaceId = selection?.id ?? null;
  const settings = useQuery({
    queryKey: ['translation-settings', workspaceId], queryFn: ({ signal }) => fetchTranslationSettings(signal), staleTime: 30_000,
  });
  const enabled = settings.data?.enabled === true;
  const settingsRevision = settings.data?.configuration_revision ?? 0;
  const targetLanguage = settings.data?.target_language ?? 'vi';
  const signature = targets.map((t) => `${t.id}@${t.revision}`).join('|');
  const scopeKey = `${workspaceId}|${type}|${settingsRevision}|${targetLanguage}|${signature}`;
  const [state, setState] = useState<{ key: string; results: ReadonlyMap<string, TranslationResult> }>({ key: '', results: EMPTY });

  useEffect(() => {
    if (!enabled || !workspaceId || targets.length === 0) return;
    const controller = new AbortController();
    const generation = workspaceGeneration();
    const expected = new Map(targets.map((t) => [t.id, t.revision]));
    // A late result is applied only while workspace generation, scope and revision still match.
    const valid = () => !controller.signal.aborted && workspaceGeneration() === generation;
    const deadline = Date.now() + MAX_POLL_MS;
    const found = new Map<string, TranslationResult>();
    const publish = () => { if (valid()) setState({ key: scopeKey, results: new Map(found) }); };
    const take = (items: Seen[]) => {
      for (const item of items) {
        if (!expected.has(item.resource_id)) continue;
        if (item.original_revision !== undefined && item.original_revision !== expected.get(item.resource_id)) continue;
        if (item.status === 'ready' && item.translation) found.set(item.resource_id, { status: 'ready', translation: item.translation });
      }
    };

    const run = async (group: TranslationTarget[]) => {
      const requests: TranslationItemRequest[] = group.map((t) => ({ resource_type: type, resource_id: t.id, resource_revision: t.revision }));
      const accepted = await submitTranslationBatch(requests, csrfToken, controller.signal);
      if (!valid()) return;
      take(accepted.items);
      publish();
      let pending = accepted.items.some((i) => i.status === 'pending');
      while (accepted.batch_id && pending && valid() && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (!valid() || Date.now() >= deadline) return;
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') continue;
        const batch = await readTranslationBatch(accepted.batch_id, controller.signal);
        if (!valid()) return;
        take(batch.items);
        publish();
        pending = batch.items.some((i) => i.status === 'pending');
      }
    };
    // Errors are swallowed on purpose: the original stays visible.
    void Promise.all(chunk(targets).map((group) => run(group).catch(() => undefined)));
    return () => controller.abort();
    // `targets` is represented by `signature` inside scopeKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, scopeKey, csrfToken]);

  const results = useMemo(() => (enabled && state.key === scopeKey ? state.results : EMPTY), [enabled, state, scopeKey]);
  return { enabled, targetLanguage, results };
}
