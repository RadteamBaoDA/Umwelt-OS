'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Input } from '@/components/ui/input';
import { TablePagination, usePagination } from '@/components/ui/pagination';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge/api';
import type { GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { parseJobPosting, type JobPosting, type JobPostingInput } from './job-posting';

/** Props for the job postings gadget. */
export interface JobListGadgetProps {
  /** Saved gadget instance and source scope. */
  instance: GadgetInstance;
}

/**
 * Parses, sorts newest first, then dedupes (by URL, else title+company) so the newest copy of a duplicate wins.
 *
 * @param items Raw document projections from the selected job sources.
 * @returns Unique postings, newest first.
 */
function buildPostings(items: JobPostingInput[]): JobPosting[] {
  const time = (p: JobPosting) => (p.postedAt ? Date.parse(p.postedAt) || 0 : 0);
  const seen = new Set<string>();
  return items.map(parseJobPosting).sort((a, b) => time(b) - time(a)).filter((p) => {
    const key = p.url ?? `${p.title}|${p.company ?? ''}`.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * Job postings merged from several job datasources as a filterable, paginated table. Read-only: it issues
 * one dashboard-projection read for the gadget's sources and reports count/freshness to the frame footer.
 * Posting links open in a new tab with `noopener`.
 */
export function JobListGadget({ instance }: JobListGadgetProps) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const sourceIds = [...new Set(instance.definition.source_ids)].slice(0, 32);
  const [filter, setFilter] = useState('');
  const query = useQuery({
    queryKey: [...documentKeys.all, 'job-list', sourceIds],
    enabled: sourceIds.length > 0,
    queryFn: () => listGadgetDocumentProjections(sourceIds),
  });
  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  const postings = useMemo(() => buildPostings(items), [items]);
  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle
      ? postings.filter((p) => [p.title, p.company, p.location].some((v) => v?.toLowerCase().includes(needle)))
      : postings;
  }, [postings, filter]);
  const pager = usePagination(visible, 20);

  useReportGadgetFreshness(
    items.reduce<string | null>((max, r) => (r.observed_at && (!max || r.observed_at > max) ? r.observed_at : max), null),
    postings.length > 0 ? t('gadgetItemCount', { count: postings.length }) : null,
  );

  const dash = '–';
  return (
    <section className="flex h-full flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
      {sourceIds.length === 0 ? <GadgetNotice kind="setup" className="p-0" message={t('gadgetDataEmpty')} />
        : query.isPending ? <GadgetSkeleton variant="table" className="p-0" label={t('gadgetDataLoading')} />
          : query.isError ? <GadgetNotice kind="error" className="p-0" message={t('gadgetDataLoadFailed')} />
            : postings.length === 0 ? <GadgetNotice kind="empty" className="p-0" message={t('gadgetDataEmpty')} />
              : (
                <>
                  <Input
                    type="search"
                    aria-label={t('jobsFilterLabel')}
                    placeholder={t('jobsFilterPlaceholder')}
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                  />
                  {visible.length === 0 ? <p role="status" className="text-sm text-muted-foreground">{t('jobsNoMatches')}</p> : (
                    <div className="min-h-0 flex-1 overflow-auto">
                      <Table aria-label={t('jobsTableLabel')} containerClassName="max-h-none">
                        <TableHeader className="sticky top-0 bg-card">
                          <TableRow>
                            <TableHead>{t('jobsColPosition')}</TableHead>
                            <TableHead>{t('jobsColCompany')}</TableHead>
                            <TableHead>{t('jobsColLocation')}</TableHead>
                            <TableHead>{t('jobsColSalary')}</TableHead>
                            <TableHead>{t('jobsColPosted')}</TableHead>
                            <TableHead>{t('jobsColSource')}</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {pager.pageItems.map((p, index) => (
                            <TableRow key={`${p.url ?? p.title}-${index}`}>
                              <TableCell className="max-w-64 whitespace-normal font-medium">
                                {p.url
                                  ? <a href={p.url} target="_blank" rel="noopener noreferrer" className="text-accent underline-offset-2 hover:underline">{p.title}</a>
                                  : p.title}
                              </TableCell>
                              <TableCell>{p.company ?? dash}</TableCell>
                              <TableCell>{p.location ?? dash}</TableCell>
                              <TableCell>{p.salary ?? dash}</TableCell>
                              <TableCell className="whitespace-nowrap tabular-nums">
                                {p.postedAt ? <time dateTime={p.postedAt}>{formatDateTime(p.postedAt, display.locale, display.timezone)}</time> : dash}
                              </TableCell>
                              <TableCell>{p.sourceLabel ?? dash}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  )}
                  <TablePagination {...pager} />
                </>
              )}
    </section>
  );
}
