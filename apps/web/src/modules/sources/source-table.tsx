'use client';

import { useTranslations } from 'next-intl';
import type { ReactNode } from 'react';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type { Source } from './api';

export type SourceRowState = 'active' | 'paused' | 'archived' | 'savedNotActive' | 'error';
export type SourceFilter = 'all' | 'active' | 'paused' | 'attention';

/**
 * Derives the visible row state from owner-reported data only.
 * "Collecting" and "Needs authorization" are not exposed by the list endpoints, so they are never invented.
 */
export function sourceRowState(source: Source, activation?: { state: string; error_code: string | null }): SourceRowState {
  if (source.status === 'archived') return 'archived';
  if (source.status === 'paused') return 'paused';
  if (source.collection_error_code || source.processing_error_code || activation?.error_code || activation?.state === 'reconciliation_required') return 'error';
  if (activation && ['saved_not_active', 'queued', 'provisioning', 'disabled'].includes(activation.state)) return 'savedNotActive';
  return 'active';
}

/** Returns whether a row state belongs to the selected filter tab. */
export function matchesFilter(state: SourceRowState, filter: SourceFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'active') return state === 'active';
  if (filter === 'paused') return state === 'paused';
  return state === 'error' || state === 'savedNotActive';
}

/** Filter tabs with loaded-row counts plus a semantic table; rows at 720px and below collapse into stacked cards. */
export function SourcesTable({ filter, counts, onFilterChange, children }: {
  filter: SourceFilter;
  counts: Record<SourceFilter, number>;
  onFilterChange: (filter: SourceFilter) => void;
  children: ReactNode;
}) {
  const t = useTranslations('sources');
  const labels: Record<SourceFilter, string> = { all: t('filterAll'), active: t('filterActive'), paused: t('filterPaused'), attention: t('filterAttention') };
  return <div className="grid gap-3">
    <Tabs value={filter} onValueChange={(value) => onFilterChange(value as SourceFilter)}>
      <TabsList aria-label={t('filterLabel')} className="h-auto max-w-full flex-wrap">
        {(Object.keys(labels) as SourceFilter[]).map((key) => <TabsTrigger key={key} value={key} className="min-h-11">{labels[key]} {counts[key]}</TabsTrigger>)}
      </TabsList>
    </Tabs>
    <table className="w-full border-collapse text-left max-[720px]:block">
      <caption className="sr-only">{t('tableCaption')}</caption>
      <thead className="max-[720px]:sr-only">
        <tr className="border-b border-border">
          {(['colSource', 'colState', 'colSchedule', 'colCollected', 'colIndexed', 'colActions'] as const).map((key) => <th key={key} scope="col" className="muted px-3 py-2 text-xs font-semibold">{t(key)}</th>)}
        </tr>
      </thead>
      <tbody className="max-[720px]:block">{children}</tbody>
    </table>
    <p className="muted text-sm">{t('neverHelp')}</p>
  </div>;
}
