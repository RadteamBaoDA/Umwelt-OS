'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { listRuns } from './api';
import { QualityPanel } from './quality-panel';
import { RunTable } from './run-table';
import { UsagePanel } from './usage-panel';

/** Mounts observability under advanced Settings without adding a navigation group or module gate. */
export function OperationsPage() {
  const t = useTranslations('observability');
  const [kind, setKind] = useState('all');
  const runs = useQuery({ queryKey: ['observability', 'runs', kind], queryFn: () => listRuns(kind), refetchInterval: 15_000 });
  /** Refetch the currently selected run-kind query; query state and errors remain with React Query. */
  const retry = () => { void runs.refetch(); };
  return <section className="space-y-6" aria-labelledby="operations-title">
    <header><h2 id="operations-title" className="text-xl font-semibold">{t('title')}</h2><p className="muted">{t('description')}</p></header>
    <div className="flex flex-wrap items-end gap-3"><label>{t('runType')}<select className="mt-1 block rounded-md border border-input bg-background px-3 py-2" value={kind} onChange={(event) => setKind(event.target.value)}><option value="all">{t('all')}</option><option value="ingestion">{t('ingestion')}</option><option value="agent">{t('agent')}</option><option value="automation">{t('automation')}</option><option value="chat">{t('chat')}</option></select></label><Button type="button" className="secondary" onClick={retry}>{t('refresh')}</Button></div>
    <RunTable runs={runs.data?.items ?? []} isLoading={runs.isPending} isError={runs.isError} retry={() => void runs.refetch()} />
    <div className="border-t border-border pt-5"><QualityPanel /></div>
    <div className="border-t border-border pt-5"><UsagePanel /></div>
  </section>;
}
