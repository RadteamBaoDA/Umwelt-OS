'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { archiveSource, connectorKeys, ConnectorActivation, deactivateConnector, purgeSource, Source, sourceKeys, updateSourceStatus } from './api';

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
