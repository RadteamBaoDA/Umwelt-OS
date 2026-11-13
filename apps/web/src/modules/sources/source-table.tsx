'use client';

import { useTranslations } from 'next-intl';
import type { ReactNode } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type { Source } from './api';

export type SourceRowState = 'active' | 'paused' | 'archived' | 'savedNotActive' | 'error' | 'reconnect';
export type SourceFilter = 'all' | 'active' | 'paused' | 'attention' | 'archived';

/**
 * Derives the visible row state from owner-reported data only.
 * "Collecting" is not exposed by the list endpoints, so it is never invented. "Reconnect needed" comes from the provider_unauthorized code.
 * n8n and MCP collectors report no auth code yet, so those sources show "Error", never "Reconnect needed".
 */
export function sourceRowState(source: Source, activation?: { state: string; error_code: string | null }): SourceRowState {
  if (source.status === 'archived') return 'archived';
  if (source.status === 'paused') return 'paused';
  if (source.collection_error_code === 'provider_unauthorized') return 'reconnect';
  if (
    source.collection_error_code ||
    source.processing_error_code ||
    activation?.error_code ||
    activation?.state === 'reconciliation_required'
  )
    return 'error';
  if (activation && ['saved_not_active', 'queued', 'provisioning', 'disabled'].includes(activation.state)) return 'savedNotActive';
  return 'active';
}

/** Returns whether a row state belongs to the selected filter tab. */
export function matchesFilter(state: SourceRowState, filter: SourceFilter): boolean {
  if (filter === 'all') return state !== 'archived';
  if (filter === 'active') return state === 'active';
  if (filter === 'paused') return state === 'paused';
  if (filter === 'archived') return state === 'archived';
  return state === 'error' || state === 'reconnect' || state === 'savedNotActive';
}

/** Filter tabs with loaded-row counts plus a semantic table; rows below 1024px collapse into stacked cards. */
export function SourcesTable({
  filter,
  counts,
  onFilterChange,
  pagination,
  children,
}: {
  filter: SourceFilter;
  counts: Record<SourceFilter, number>;
  onFilterChange: (filter: SourceFilter) => void;
  /** Pager rendered right under the table. */
  pagination?: ReactNode;
  children: ReactNode;
}) {
  const t = useTranslations('sources');
  const labels: Record<SourceFilter, string> = {
    all: t('filter.all'),
    active: t('filter.active'),
    paused: t('filter.paused'),
    attention: t('filter.attention'),
    archived: t('filter.archived'),
  };
  return (
    <div className="grid gap-3">
      {/* Below 640px a Select replaces the tabs (same state) so no filter is hidden behind horizontal scroll. */}
      <Select value={filter} onValueChange={(value) => onFilterChange(value as SourceFilter)}>
        <SelectTrigger aria-label={t('filter.label')} className="w-full sm:hidden">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(Object.keys(labels) as SourceFilter[]).map((key) => (
            <SelectItem key={key} value={key}>
              {labels[key]} {counts[key]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Tabs value={filter} onValueChange={(value) => onFilterChange(value as SourceFilter)} className="max-sm:hidden">
        <TabsList aria-label={t('filter.label')} className="h-auto max-w-full justify-start overflow-x-auto [scrollbar-width:none]">
          {(Object.keys(labels) as SourceFilter[]).map((key) => (
            <TabsTrigger key={key} value={key} className="min-h-9 shrink-0 pointer-coarse:min-h-11">
              {labels[key]} {counts[key]}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      <Table className="border-collapse max-[1023px]:block" containerClassName="max-[1023px]:max-h-none">
        <caption className="sr-only">{t('table.caption')}</caption>
        <TableHeader className="max-[1023px]:sr-only">
          <TableRow className="border-b border-border">
            {(['col.source', 'col.state', 'col.schedule', 'col.collected', 'col.indexed', 'col.actions'] as const).map((key) => (
              <TableHead
                key={key}
                scope="col"
                className="text-muted-foreground px-3 py-2 text-sm font-medium min-[1024px]:whitespace-nowrap"
              >
                {t(key)}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody className="max-[1023px]:block">{children}</TableBody>
      </Table>
      {pagination}
      <p className="text-muted-foreground text-sm">{t('neverHelp')}</p>
    </div>
  );
}
