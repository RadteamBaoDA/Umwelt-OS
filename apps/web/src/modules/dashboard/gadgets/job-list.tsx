'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Input } from '@/components/ui/input';
import { TablePagination, usePagination } from '@/components/ui/pagination';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';
import type { GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { ShowMoreButton, useShowMore } from './show-more';
import { parseJobPosting, type JobPosting, type JobPostingInput } from './job-posting';
import {
  CollectionTranslationBar,
  ItemTranslateButton,
  TranslationStatusBanner,
  useItemTranslation,
  type ItemTranslationController,
} from '@/modules/translations';
import { useCollectionTranslateAll } from '../gadget-actions';

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
  return items
    .map(parseJobPosting)
    .sort((a, b) => time(b) - time(a))
    .filter((p) => {
      const key = p.url ?? `${p.title}|${p.company ?? ''}`.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

/**
 * Scrollable job table for the current page with a show-more control.
 *
 * @param p.more Show-more state over the current page's postings (first rows plus expansion).
 * @returns Table of postings; links open in a new tab with `noopener noreferrer`; missing fields show an en dash.
 */
function JobTable({ more, translation }: { more: ReturnType<typeof useShowMore<JobPosting>>; translation: ItemTranslationController }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const dash = '–';
  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <Table aria-label={t('jobs.tableLabel')} containerClassName="max-h-none">
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            <TableHead>{t('jobs.colPosition')}</TableHead>
            <TableHead>{t('jobs.colCompany')}</TableHead>
            <TableHead>{t('jobs.colLocation')}</TableHead>
            <TableHead>{t('jobs.colSalary')}</TableHead>
            <TableHead>{t('jobs.colPosted')}</TableHead>
            <TableHead>{t('jobs.colSource')}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {more.visible.map((p, index) => {
            const displayTitle = (p.documentId ? translation.shown(p.documentId)?.title : undefined) ?? p.title;
            return (
              <TableRow key={`${p.url ?? p.title}-${index}`} className="group/item">
                <TableCell className="max-w-64 whitespace-normal font-medium">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {p.url ? (
                      <a href={p.url} target="_blank" rel="noopener noreferrer" className="text-data underline-offset-2 hover:underline">
                        {displayTitle}
                      </a>
                    ) : (
                      <span>{displayTitle}</span>
                    )}
                    {p.documentId && p.documentVersionId ? (
                      <ItemTranslateButton
                        controller={translation}
                        target={{ id: p.documentId, revision: p.documentVersionId }}
                        title={displayTitle}
                      />
                    ) : null}
                  </div>
                </TableCell>
                <TableCell>{p.company ?? dash}</TableCell>
                <TableCell>{p.location ?? dash}</TableCell>
                <TableCell>{p.salary ?? dash}</TableCell>
                <TableCell className="whitespace-nowrap tabular-nums">
                  {p.postedAt ? <time dateTime={p.postedAt}>{formatDateTime(p.postedAt, display.locale, display.timezone)}</time> : dash}
                </TableCell>
                <TableCell>{p.sourceLabel ?? dash}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <div className="flex justify-center py-1">
        <ShowMoreButton state={more} />
      </div>
    </div>
  );
}

/**
 * Job postings merged from several job datasources as a filterable, paginated table. Read-only: it issues
 * one dashboard-projection read for the gadget's sources and reports count/freshness to the frame footer.
 * Posting links open in a new tab with `noopener`.
 */
export function JobListGadget({ instance }: JobListGadgetProps) {
  const t = useTranslations('dashboard');
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
    return needle ? postings.filter((p) => [p.title, p.company, p.location].some((v) => v?.toLowerCase().includes(needle))) : postings;
  }, [postings, filter]);
  const pager = usePagination(visible, 20);
  // First 5 rows of the current page; expanding happens inside the scroll area, and filter/page changes collapse it.
  const more = useShowMore(pager.pageItems, 5, `${filter}|${pager.page}|${pager.pageSize}`);
  const translationTargets = useMemo(
    () =>
      more.visible
        .filter((p): p is JobPosting & { documentId: string; documentVersionId: string } => Boolean(p.documentId && p.documentVersionId))
        .map((p) => ({ id: p.documentId, revision: p.documentVersionId })),
    [more.visible],
  );
  const translation = useItemTranslation('document', translationTargets);
  useCollectionTranslateAll(translation);

  useReportGadgetFreshness(
    items.reduce<string | null>((max, r) => (r.observed_at && (!max || r.observed_at > max) ? r.observed_at : max), null),
    postings.length > 0 ? t('gadget.itemCount', { count: postings.length }) : null,
  );

  return (
    <section className="flex h-full flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
      {sourceIds.length === 0 ? (
        <GadgetNotice
          kind="setup"
          className="p-0"
          message={t('gadget.dataEmpty')}
          action={{ href: '/settings/sources', label: t('gadget.configureAction') }}
        />
      ) : query.isPending ? (
        <GadgetSkeleton variant="table" className="p-0" label={t('gadget.dataLoading')} />
      ) : query.isError ? (
        <GadgetNotice kind="error" className="p-0" message={t('gadget.dataLoadFailed')} />
      ) : postings.length === 0 ? (
        <GadgetNotice kind="empty" className="p-0" message={t('gadget.dataEmpty')} />
      ) : (
        <>
          <Input
            type="search"
            aria-label={t('jobs.filterLabel')}
            placeholder={t('jobs.filterPlaceholder')}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <CollectionTranslationBar controller={translation} />
          <TranslationStatusBanner issues={translation.issues} />
          {visible.length === 0 ? (
            <p role="status" className="text-sm text-muted-foreground">
              {t('jobs.noMatches')}
            </p>
          ) : (
            <JobTable more={more} translation={translation} />
          )}
          <TablePagination {...pager} />
        </>
      )}
    </section>
  );
}
