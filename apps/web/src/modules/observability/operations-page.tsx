'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
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
    <header><h2 id="operations-title" className="text-xl font-semibold">{t('title')}</h2><p className="text-muted-foreground">{t('description')}</p></header>
    <div className="flex flex-wrap items-end gap-3"><div className="flex flex-col"><span id="run-type-label">{t('runType')}</span><Select value={kind} onValueChange={setKind}><SelectTrigger aria-labelledby="run-type-label" className="mt-1 w-44"><SelectValue /></SelectTrigger><SelectContent>{(['all', 'ingestion', 'agent', 'automation', 'chat'] as const).map((value) => <SelectItem key={value} value={value}>{t(value)}</SelectItem>)}</SelectContent></Select></div><Button type="button" variant="outline" onClick={retry}>{t('refresh')}</Button></div>
    <RunTable runs={runs.data?.items ?? []} isLoading={runs.isPending} isError={runs.isError} retry={() => void runs.refetch()} />
    <div className="border-t border-border pt-5"><QualityPanel /></div>
    <div className="border-t border-border pt-5"><UsagePanel /></div>
  </section>;
}
