'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import { readAccountDeletionStatus } from './deletion-api';

const phaseMessages = {
  queued: 'phase.queued', auxiliary: 'phase.auxiliary', remote: 'phase.remote', graph: 'phase.graph',
  storage: 'phase.storage', shared: 'phase.shared', database: 'phase.database',
  references: 'phase.references', verify: 'phase.verify', complete: 'phase.complete',
} as const;
const failureMessages = {
  auxiliary_cleanup_failed: 'failure.auxiliary', remote_cleanup_failed: 'failure.remote',
  graph_cleanup_failed: 'failure.graph', storage_cleanup_failed: 'failure.storage',
  shared_cleanup_failed: 'failure.shared', data_cleanup_failed: 'failure.data',
  cleanup_retry_required: 'failure.unknown',
} as const;

/** Poll the restricted receipt and display finite, translated progress categories only.
 * Unknown server values never become message keys or visible account/error content.
 */
export function AccountDeletionStatus() {
  const s0 = useAccountDeletionStatusController();

  const vm: AccountDeletionStatusVm = s0;
  return <main className="mx-auto grid min-h-dvh max-w-2xl content-start gap-6 px-6 py-12">
<AccountDeletionStatusHeader vm={vm} />
<AccountDeletionStatusDetails vm={vm} />
  </main>;
}

type AccountDeletionStatusVm = ReturnType<typeof useAccountDeletionStatusController>;

/** Heading, help text and the live progress region. */
function AccountDeletionStatusHeader({ vm }: { vm: AccountDeletionStatusVm }) {
  const { t, query, statusKey, phaseKey, failureKey } = vm;
  return (
<>
    <h1 className="text-2xl font-semibold">{t('status.title')}</h1>
    <p className="text-muted-foreground">{t('status.help')}</p>
    <div role="status" aria-live="polite">
      <p>{query.isPending ? t('checking') : statusKey ? t(statusKey) : query.data ? t('status.unavailable') : null}</p>
      {query.data && <p>{t('phase.label', { phase: t(phaseKey) })}</p>}
      {query.data?.status === 'failed' && <p>{t('failure.label', { category: t(failureKey) })}</p>}
    </div>
</>
  );
}

/** Failure, progress, limits and the retry and privacy actions. */
function AccountDeletionStatusDetails({ vm }: { vm: AccountDeletionStatusVm }) {
  const { t, query } = vm;
  return (
<>
    {query.isError && <p role="alert">{query.error instanceof ApiError && query.error.status === 404 ? t('noReceipt') : t('status.unavailable')}</p>}
    {query.data && <p>{t('progress')}</p>}
    <p className="text-sm text-muted-foreground">{t('limits')}</p>
    {query.data?.status !== 'succeeded' && <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('retry')}</Button>}
    <Button asChild variant="link"><Link href="/privacy">{t('privacy')}</Link></Button>
</>
  );
}

/** Holds the state, queries and derived values of AccountDeletionStatus. */
function useAccountDeletionStatusController() {
  const t = useTranslations('accountDeletion');
  const query = useQuery({
    queryKey: ['account-deletion-status'],
    queryFn: ({ signal }) => readAccountDeletionStatus(signal),
    retry: false,
    refetchInterval: (state) => state.state.data?.status === 'succeeded' || (state.state.error instanceof ApiError && state.state.error.status === 404) ? false : 3_000,
  });
  const statusKey: 'cleanupFailed' | 'queued' | 'running' | 'succeeded' | null = query.data?.status === 'failed' ? 'cleanupFailed'
    : query.data?.status === 'queued' || query.data?.status === 'running' || query.data?.status === 'succeeded' ? query.data.status : null;
  const phaseKey: (typeof phaseMessages)[keyof typeof phaseMessages] | 'phase.unknown' = query.data && Object.hasOwn(phaseMessages, query.data.phase)
    ? phaseMessages[query.data.phase as keyof typeof phaseMessages] : 'phase.unknown';
  const failureKey: (typeof failureMessages)[keyof typeof failureMessages] | 'failure.unknown' = query.data?.error_code && Object.hasOwn(failureMessages, query.data.error_code)
    ? failureMessages[query.data.error_code as keyof typeof failureMessages] : 'failure.unknown';

  return { t, query, statusKey, phaseKey, failureKey };
}
