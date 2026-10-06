'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { documentDeletionKeys, getDocumentDeletionOperation, type DocumentDeletionReceipt } from './api';

type DocumentDeletionStageStatus =
  | DocumentDeletionReceipt['status']
  | DocumentDeletionReceipt['raw_status']
  | DocumentDeletionReceipt['evidence_scope_status']
  | DocumentDeletionReceipt['copied_status']
  | DocumentDeletionReceipt['chat_status'];

/** Shows the operation-only deletion receipt after its Document and content queries are gone. */
export function DocumentDeletionReceiptPanel({ operationId }: { operationId: string }) {
  const t = useTranslations('shell');
  const pollUntil = useRef(Date.now() + 30_000);
  const [automaticRefreshPaused, setAutomaticRefreshPaused] = useState(false);
  useEffect(() => {
    pollUntil.current = Date.now() + 30_000;
    setAutomaticRefreshPaused(false);
    const timer = window.setTimeout(() => setAutomaticRefreshPaused(true), 30_000);
    return () => window.clearTimeout(timer);
  }, [operationId]);
  const operation = useQuery({
    queryKey: documentDeletionKeys.detail(operationId),
    queryFn: ({ signal }) => getDocumentDeletionOperation(operationId, signal),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      const active = status === 'queued' || status === 'running';
      // Aggregate status can stay running for owners without a UI stage; stop polling at the deadline and leave GET refresh manual.
      return active && Date.now() < pollUntil.current ? 3_000 : false;
    },
    refetchIntervalInBackground: false,
  });

  /** Maps exact API stage states to localized labels without treating shared or aggregate-pending data as erased. */
  function stageLabel(status: DocumentDeletionStageStatus): string {
    switch (status) {
      case 'queued': return t('documentDeletionQueued');
      case 'running': return t('documentDeletionRunning');
      case 'succeeded': return t('documentDeletionSucceeded');
      case 'failed': return t('documentDeletionFailed');
      case 'not_present': return t('documentDeletionNotPresent');
      case 'retained_shared': return t('documentDeletionShared');
      case 'capturing': return t('documentDeletionCapturing');
      case 'captured': return t('documentDeletionCaptured');
      case 'unavailable': return t('documentDeletionUnavailable');
    }
  }

  const receipt = operation.data?.operation_id === operationId ? operation.data : undefined;
  const scopeUnavailable = receipt?.evidence_scope_status === 'unavailable';
  const summary = scopeUnavailable
    ? t('documentDeletionScopeFailure')
    : receipt?.status === 'queued'
    ? t('documentDeletionAggregateQueued')
    : receipt?.status === 'running'
      ? t('documentDeletionAggregateRunning')
      : receipt?.status === 'failed'
        ? t('documentDeletionAggregateFailed')
        : receipt?.status === 'succeeded'
          ? t('documentDeletionAggregateSucceeded')
          : t('documentDeletionLoading');

  return <section aria-labelledby="document-deletion-title" className="rounded-lg border border-border bg-card p-4 text-card-foreground shadow-sm">
    <h1 id="document-deletion-title" className="text-lg font-semibold">{t('documentDeletionTitle')}</h1>
    {receipt?.immediate_access_revoked === true && <p className="mt-2 text-sm text-muted-foreground">{t('documentDeletionAccessRevoked')}</p>}
    <p className="mt-3" role={receipt?.status === 'failed' || scopeUnavailable ? 'alert' : 'status'} aria-live={receipt?.status === 'failed' || scopeUnavailable ? 'assertive' : 'polite'}>{summary}</p>
    {operation.isPending && <p className="mt-2 text-sm text-muted-foreground" role="status">{t('documentDeletionLoading')}</p>}
    {operation.isError && <p className="mt-2 text-sm text-destructive" role="alert">{receipt ? t('documentDeletionRefreshFailed') : t('documentDeletionStatusUnavailable')}</p>}
    {automaticRefreshPaused && receipt && (receipt.status === 'queued' || receipt.status === 'running') && <p className="mt-2 text-sm text-muted-foreground" role="status">{t('documentDeletionAutomaticRefreshPaused')}</p>}
    {receipt && <>
      <p className="mt-3 text-sm text-muted-foreground">{t('documentDeletionOperation')}: <code className="break-all">{receipt.operation_id}</code></p>
      <dl aria-live="polite" className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="rounded-md border border-border p-3"><dt className="text-sm font-medium">{t('documentDeletionRaw')}</dt><dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.raw_status)}</dd></div>
        <div className="rounded-md border border-border p-3"><dt className="text-sm font-medium">{t('documentDeletionEvidenceScope')}</dt><dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.evidence_scope_status)}</dd></div>
        <div className="rounded-md border border-border p-3"><dt className="text-sm font-medium">{t('documentDeletionCopied')}</dt><dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.copied_status)}</dd></div>
        <div className="rounded-md border border-border p-3"><dt className="text-sm font-medium">{t('documentDeletionChat')}</dt><dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.chat_status)}</dd></div>
      </dl>
    </>}
    <div className="mt-4 flex flex-wrap gap-3">
      <Button type="button" className="secondary" disabled={operation.isFetching} onClick={() => void operation.refetch()}>
        {operation.isFetching ? t('documentDeletionRefreshing') : t('documentDeletionRefresh')}
      </Button>
      <Link className="inline-flex min-h-11 items-center rounded-md px-3 text-sm underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" href="/knowledge/documents">{t('documentDeletionBackToList')}</Link>
    </div>
  </section>;
}
