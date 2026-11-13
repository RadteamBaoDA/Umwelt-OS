'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { documentDeletionKeys, getDocumentDeletionOperation, type DocumentDeletionReceipt } from './api';
import { useResetOnChange } from '@/modules/shared';

type DocumentDeletionStageStatus =
  | DocumentDeletionReceipt['status']
  | DocumentDeletionReceipt['raw_status']
  | DocumentDeletionReceipt['evidence_scope_status']
  | DocumentDeletionReceipt['copied_status']
  | DocumentDeletionReceipt['chat_status']
  | DocumentDeletionReceipt['memory_status']
  | DocumentDeletionReceipt['agent_status']
  | DocumentDeletionReceipt['materialization_status']
  | DocumentDeletionReceipt['brief_status'];

/** Shows the operation-only deletion receipt after its Document and content queries are gone. */
// eslint-disable-next-line complexity, max-lines-per-function -- stateful workflow whose branches mirror independent UI/request states (loading, error, conflict, per-step content); extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function DocumentDeletionReceiptPanel({ operationId }: { operationId: string }) {
  const t = useTranslations('chat');
  const pollUntil = useRef(0);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  const [automaticRefreshPaused, setAutomaticRefreshPaused] = useState(false);
  useResetOnChange([operationId], () => {
    setAutomaticRefreshPaused(false);
  });
  useEffect(() => {
    pollUntil.current = Date.now() + 30_000;
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
      case 'queued':
        return t('documentDeletion.queued');
      case 'running':
        return t('documentDeletion.running');
      case 'succeeded':
        return t('documentDeletion.succeeded');
      case 'failed':
        return t('documentDeletion.failed');
      case 'not_present':
        return t('documentDeletion.notPresent');
      case 'retained_shared':
        return t('documentDeletion.shared');
      case 'capturing':
        return t('documentDeletion.capturing');
      case 'captured':
        return t('documentDeletion.captured');
      case 'unavailable':
        return t('documentDeletion.unavailable');
    }
  }

  const receipt = operation.data?.operation_id === operationId ? operation.data : undefined;
  const scopeUnavailable = receipt?.evidence_scope_status === 'unavailable';
  const memoryUnresolved = receipt?.memory_error_code === 'legacy_provenance_unresolved';
  const agentUnresolved = receipt?.agent_status === 'failed' && receipt.agent_unresolved_count > 0;
  const materializationUnresolved = receipt?.materialization_status === 'failed' && receipt.materialization_unresolved_count > 0;
  const briefUnresolved = receipt?.brief_status === 'failed' && receipt.brief_unresolved_count > 0;
  const summary = agentUnresolved
    ? t('documentDeletion.agentUnresolved')
    : materializationUnresolved
      ? t('documentDeletion.materializationUnresolved')
      : briefUnresolved
        ? t('documentDeletion.briefUnresolved')
        : memoryUnresolved
          ? t('documentDeletion.memoryUnresolved')
          : scopeUnavailable
            ? t('documentDeletion.scopeFailure')
            : receipt?.status === 'queued'
              ? t('documentDeletion.aggregateQueued')
              : receipt?.status === 'running'
                ? t('documentDeletion.aggregateRunning')
                : receipt?.status === 'failed'
                  ? t('documentDeletion.aggregateFailed')
                  : receipt?.status === 'succeeded'
                    ? t('documentDeletion.aggregateSucceeded')
                    : t('documentDeletion.loading');

  return (
    <section aria-labelledby="document-deletion-title" className="rounded-lg border border-border bg-card p-4 text-card-foreground">
      <h1
        id="document-deletion-title"
        ref={headingRef}
        tabIndex={-1}
        className="text-lg font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {t('documentDeletion.title')}
      </h1>
      {receipt?.immediate_access_revoked === true && (
        <p className="mt-2 text-sm text-muted-foreground">{t('documentDeletion.accessRevoked')}</p>
      )}
      <p
        className="mt-3"
        role={
          receipt?.status === 'failed' ||
          scopeUnavailable ||
          memoryUnresolved ||
          agentUnresolved ||
          materializationUnresolved ||
          briefUnresolved
            ? 'alert'
            : 'status'
        }
        aria-live={
          receipt?.status === 'failed' ||
          scopeUnavailable ||
          memoryUnresolved ||
          agentUnresolved ||
          materializationUnresolved ||
          briefUnresolved
            ? 'assertive'
            : 'polite'
        }
      >
        {summary}
      </p>
      {receipt?.memory_unresolved_count ? (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {t('documentDeletion.memoryUnresolvedCount', { count: receipt.memory_unresolved_count })}
        </p>
      ) : null}
      {receipt?.memory_cache_pending && (
        <p className="mt-2 text-sm text-muted-foreground" role="status">
          {t('documentDeletion.memoryCachePending')}
        </p>
      )}
      {receipt?.agent_unresolved_count ? (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {t('documentDeletion.agentUnresolvedCount', { count: receipt.agent_unresolved_count })}
        </p>
      ) : null}
      {receipt?.materialization_unresolved_count ? (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {t('documentDeletion.materializationUnresolvedCount', { count: receipt.materialization_unresolved_count })}
        </p>
      ) : null}
      {receipt?.brief_unresolved_count ? (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {t('documentDeletion.briefUnresolvedCount', { count: receipt.brief_unresolved_count })}
        </p>
      ) : null}
      {receipt?.agent_waiting_for_lease && (
        <p className="mt-2 text-sm text-muted-foreground" role="status">
          {t('documentDeletion.agentLeasePending')}
        </p>
      )}
      {operation.isPending && (
        <p className="mt-2 text-sm text-muted-foreground" role="status">
          {t('documentDeletion.loading')}
        </p>
      )}
      {operation.isError && (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {receipt ? t('documentDeletion.refreshFailed') : t('documentDeletion.statusUnavailable')}
        </p>
      )}
      {automaticRefreshPaused && receipt && (receipt.status === 'queued' || receipt.status === 'running') && (
        <p className="mt-2 text-sm text-muted-foreground" role="status">
          {t('documentDeletion.automaticRefreshPaused')}
        </p>
      )}
      {receipt && (
        <>
          <p className="mt-3 text-sm text-muted-foreground">
            {t('documentDeletion.operation')}: <code className="break-all">{receipt.operation_id}</code>
          </p>
          <dl aria-live="polite" className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.raw')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.raw_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.evidenceScope')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.evidence_scope_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.copied')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.copied_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.chat')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.chat_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.memory')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.memory_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.agent')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.agent_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.materialization')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.materialization_status)}</dd>
            </div>
            <div className="rounded-md border border-border p-3">
              <dt className="text-sm font-medium">{t('documentDeletion.brief')}</dt>
              <dd className="mt-1 text-sm text-muted-foreground">{stageLabel(receipt.brief_status)}</dd>
            </div>
          </dl>
        </>
      )}
      <div className="mt-4 flex flex-wrap gap-3">
        <Button type="button" variant="outline" disabled={operation.isFetching} onClick={() => void operation.refetch()}>
          {operation.isFetching ? t('documentDeletion.refreshing') : t('documentDeletion.refresh')}
        </Button>
        <Link
          className="inline-flex min-h-9 pointer-coarse:min-h-11 items-center rounded-md px-3 text-sm underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          href="/knowledge/documents"
        >
          {t('documentDeletion.backToList')}
        </Link>
      </div>
    </section>
  );
}
