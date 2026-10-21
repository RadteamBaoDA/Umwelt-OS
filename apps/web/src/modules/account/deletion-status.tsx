'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import { readAccountDeletionStatus } from './deletion-api';

const phaseMessages = {
  queued: 'phaseQueued', auxiliary: 'phaseAuxiliary', remote: 'phaseRemote', graph: 'phaseGraph',
  storage: 'phaseStorage', shared: 'phaseShared', database: 'phaseDatabase',
  references: 'phaseReferences', verify: 'phaseVerify', complete: 'phaseComplete',
} as const;
const failureMessages = {
  auxiliary_cleanup_failed: 'failureAuxiliary', remote_cleanup_failed: 'failureRemote',
  graph_cleanup_failed: 'failureGraph', storage_cleanup_failed: 'failureStorage',
  shared_cleanup_failed: 'failureShared', data_cleanup_failed: 'failureData',
  cleanup_retry_required: 'failureUnknown',
} as const;

/** Poll the restricted receipt and display finite, translated progress categories only.
 * Unknown server values never become message keys or visible account/error content.
 */
export function AccountDeletionStatus() {
  const t = useTranslations('accountDeletion');
  const query = useQuery({
    queryKey: ['account-deletion-status'],
    queryFn: ({ signal }) => readAccountDeletionStatus(signal),
    retry: false,
    refetchInterval: (state) => state.state.data?.status === 'succeeded' || (state.state.error instanceof ApiError && state.state.error.status === 404) ? false : 3_000,
  });
  const statusKey = query.data?.status === 'failed' ? 'cleanupFailed'
    : query.data?.status === 'queued' || query.data?.status === 'running' || query.data?.status === 'succeeded' ? query.data.status : null;
  const phaseKey = query.data && Object.hasOwn(phaseMessages, query.data.phase)
    ? phaseMessages[query.data.phase as keyof typeof phaseMessages] : 'phaseUnknown';
  const failureKey = query.data?.error_code && Object.hasOwn(failureMessages, query.data.error_code)
    ? failureMessages[query.data.error_code as keyof typeof failureMessages] : 'failureUnknown';
  return <main className="mx-auto grid min-h-dvh max-w-2xl content-start gap-6 px-6 py-12">
    <h1 className="text-2xl font-semibold">{t('statusTitle')}</h1>
    <p className="text-muted-foreground">{t('statusHelp')}</p>
    <div role="status" aria-live="polite">
      <p>{query.isPending ? t('checking') : statusKey ? t(statusKey) : query.data ? t('statusUnavailable') : null}</p>
      {query.data && <p>{t('phaseLabel', { phase: t(phaseKey) })}</p>}
      {query.data?.status === 'failed' && <p>{t('failureLabel', { category: t(failureKey) })}</p>}
    </div>
    {query.isError && <p role="alert">{query.error instanceof ApiError && query.error.status === 404 ? t('noReceipt') : t('statusUnavailable')}</p>}
    {query.data && <p>{t('progress')}</p>}
    <p className="text-sm text-muted-foreground">{t('limits')}</p>
    {query.data?.status !== 'succeeded' && <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('retry')}</Button>}
    <Button asChild variant="link"><Link href="/privacy">{t('privacy')}</Link></Button>
  </main>;
}
