'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { archiveSource, CollectionRequest, CollectionStatus, connectorKeys, ConnectorActivation, deactivateConnector, getCollectionRequest, getConnectorActivation, ManualSyncResult, purgeSource, Source, sourceKeys, triggerCollection, updateSourceStatus } from './api';

const TERMINAL: ReadonlySet<string> = new Set(['succeeded', 'no_changes', 'failed', 'cancelled']);
export const COLLECTION_POLL_MS = 2000;

/** Polls a collection request every `intervalMs` until it is terminal; resolves null when aborted. A fetch error ends the poll by throwing. */
export async function pollCollectionRequest(
  fetchRequest: (signal: AbortSignal) => Promise<CollectionRequest>,
  { signal, intervalMs = COLLECTION_POLL_MS, onUpdate }: { signal: AbortSignal; intervalMs?: number; onUpdate?: (request: CollectionRequest) => void },
): Promise<CollectionRequest | null> {
  while (!signal.aborted) {
    const request = await fetchRequest(signal);
    if (signal.aborted) return null;
    onUpdate?.(request);
    if (TERMINAL.has(request.status)) return request;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, intervalMs);
      signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
    });
  }
  return null;
}

/**
 * Polls connector activation until it is `active`, fails hard, or the timeout passes.
 * Hard failures are `reconciliation_required` or `saved_not_active` carrying an error code. Resolves null when aborted.
 */
export async function waitForActivation(
  sourceId: string,
  signal: AbortSignal,
  { timeoutMs = 60_000, intervalMs = COLLECTION_POLL_MS }: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<{ ok: boolean; code: string | null } | null> {
  const deadline = Date.now() + timeoutMs;
  while (!signal.aborted) {
    const activation = await getConnectorActivation(sourceId, signal);
    if (signal.aborted) return null;
    if (activation.state === 'active') return { ok: true, code: null };
    if (activation.state === 'reconciliation_required' || (activation.state === 'saved_not_active' && activation.error_code)) return { ok: false, code: activation.error_code ?? activation.state };
    if (Date.now() >= deadline) return { ok: false, code: 'activation_timeout' };
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, intervalMs);
      signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
    });
  }
  return null;
}

/**
 * Triggers one collection for a source and polls its durable request to a terminal state.
 * `onAccepted` sees the 202 body before polling; `onUpdate` sees every polled status. Resolves null when aborted.
 * A response without a request id (synchronous path) is mapped to a terminal request so callers have one result shape.
 * Errors from the trigger or from polling reject; callers own the user-facing message.
 */
export async function collectAndWait(
  sourceId: string,
  csrfToken: string,
  { signal, intervalMs, onAccepted, onUpdate }: { signal: AbortSignal; intervalMs?: number; onAccepted?: (result: ManualSyncResult) => void; onUpdate?: (request: CollectionRequest) => void },
): Promise<CollectionRequest | null> {
  const result = await triggerCollection(sourceId, csrfToken);
  if (signal.aborted) return null;
  onAccepted?.(result);
  const requestId = result.request_id;
  if (!requestId) {
    // No durable request to poll: trust only a recognised terminal status, otherwise report a failure code.
    const known = TERMINAL.has(result.status);
    return { request_id: '', source_id: sourceId, status: (known ? result.status : 'failed') as CollectionStatus, ingestion_run_id: result.run_id, error_code: known ? null : 'no_request_id' };
  }
  return pollCollectionRequest((s) => getCollectionRequest(sourceId, requestId, s), { signal, intervalMs, onUpdate });
}

/** Shared Pause/Resume and Disconnect handlers used by both the source list rows and the connector editor header. */
export function useSourceActions({ source, activation, onResumed, onChanged, onPurgeStarted }: {
  source: Source;
  activation?: ConnectorActivation;
  onResumed?: (source: Source) => void;
  onChanged: () => void;
  onPurgeStarted?: (operationId: string) => void;
}) {
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const connector = ['rss', 'web', 'api'].includes(source.type);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  /** Changes the source between active and paused states and refreshes the displayed source data. */
  async function toggleStatus() {
    setBusy(true);
    setError(false);
    try {
      if (source.status === 'paused') {
        const resumed = await updateSourceStatus(source.id, 'active', csrfToken);
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onResumed?.(resumed);
      } else if (connector && activation && activation.desired_revision > 0) {
        await deactivateConnector(source.id, csrfToken);
        await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(source.id) });
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onChanged();
      } else {
        await updateSourceStatus(source.id, 'paused', csrfToken);
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onChanged();
      }
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  /** Archives the source and optionally starts its data purge, preserving the user-selected deletion behavior. */
  async function disconnect(deleteData: boolean) {
    setBusy(true);
    setError(false);
    try {
      if (deleteData) {
        const operation = await purgeSource(source.id, csrfToken);
        onPurgeStarted?.(operation.operation_id);
      } else {
        await archiveSource(source.id, csrfToken);
      }
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(source.id) });
      onChanged();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  return { busy, error, toggleStatus, disconnect };
}
