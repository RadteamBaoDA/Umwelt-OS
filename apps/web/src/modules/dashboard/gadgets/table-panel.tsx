'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { useChatController } from '@/core/app-shell/chat-controller';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { cleanExcerpt } from '@/modules/news';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import type { FeedDocument } from './feed-model';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';

/** Props for the source-backed table gadget. */
export interface TablePanelProps {
  /** Saved gadget instance and source scope. */
  instance: GadgetInstance;
}

type SelectedRecordIdentity = {
  sourceId: string;
  documentId: string;
  documentVersionId: string;
  versionNumber: number;
};

const SEVERITY = /^(.*?)[\s:|\-–—]*\b(LOW|MEDIUM|HIGH|CRITICAL)\s*$/;

/** Splits a trailing all-caps severity word from a record title so it can render as a badge. */
function splitSeverity(title: string): { title: string; severity?: 'low' | 'medium' | 'high' | 'critical' } {
  const match = SEVERITY.exec(title);
  return match && match[1] ? { title: match[1], severity: match[2].toLowerCase() as 'low' | 'medium' | 'high' | 'critical' } : { title };
}

/**
 * Excerpt to show under a title, or null when it only repeats the title (feeds such as Google News echo the headline,
 * differing only in case, punctuation or spacing). Compared on lowercase letters/digits only.
 *
 * @param title Record title.
 * @param excerpt Stored excerpt.
 * @returns Cleaned excerpt text, or null when empty or contained in the title.
 */
function distinctExcerpt(title: string, excerpt: string | null | undefined): string | null {
  const text = cleanExcerpt(title, excerpt);
  const fold = (value: string) => value.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  const folded = text ? fold(text) : '';
  return !text || !folded || fold(title).includes(folded) ? null : text;
}

/** One stacked record row: optional selection checkbox, title with severity badge, distinct excerpt and date. */
function TableRecordRow({ record, selectMode, checked, setSelectedRecords, toggle }: {
  record: FeedDocument;
  selectMode: boolean;
  checked: boolean;
  setSelectedRecords: React.Dispatch<React.SetStateAction<Record<string, SelectedRecordIdentity>>>;
  /** The select-mode toggle, rendered in the first row at rest. */
  toggle: React.ReactNode;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const shown = splitSeverity(record.title);
  const excerpt = distinctExcerpt(record.title, record.excerpt);
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-0.5 py-2">
      {selectMode && (
        <Checkbox
          aria-label={t('selectRecord', { title: record.title })}
          checked={checked}
          onCheckedChange={(next) => setSelectedRecords((current) => {
            if (next !== true) {
              const remaining = { ...current };
              delete remaining[record.document_id];
              return remaining;
            }
            if (Object.keys(current).length >= 32) return current;
            return { ...current, [record.document_id]: {
              sourceId: record.source_id,
              documentId: record.document_id,
              documentVersionId: record.document_version_id,
              versionNumber: record.version_number,
            } };
          })}
        />
      )}
      <div className="min-w-0 flex-1 basis-48">
        <p className="text-sm font-medium leading-snug">{shown.title}{shown.severity && <Badge variant="outline" className="ml-2 align-middle">{t(`severity_${shown.severity}`)}</Badge>}</p>
        {excerpt && <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{excerpt}</p>}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-0.5">
        {toggle}
        <time className="whitespace-nowrap text-xs tabular-nums text-muted-foreground" dateTime={record.published_at ?? record.observed_at ?? undefined}>{formatDateTime(record.published_at ?? record.observed_at, display.locale, display.timezone)}</time>
      </div>
    </li>
  );
}

/**
 * Render current source records as stacked rows in a bounded scroll root. The count and freshness go to the
 * frame footer; selection (and Ask AI with the exact selected versions, max 32) is behind a selection-mode toggle.
 */
export function TablePanel({ instance }: TablePanelProps) {
  const t = useTranslations('dashboard');
  const { openDrawer } = useChatController();
  const sourceIds = [...new Set(instance.definition.source_ids)].slice(0, 32);
  // Row checkboxes and the Ask AI action exist only in selection mode.
  const [selectMode, setSelectMode] = useState(false);
  const [selectedRecords, setSelectedRecords] = useState<Record<string, SelectedRecordIdentity>>({});
  const query = useQuery({
    queryKey: [...documentKeys.all, 'table-panel', sourceIds],
    enabled: sourceIds.length > 0,
    queryFn: () => listGadgetDocumentProjections(sourceIds),
  });
  const records = useMemo(() => query.data?.items ?? [], [query.data]);
  const selected = Object.values(selectedRecords);
  const selectedIds = new Set(Object.keys(selectedRecords));
  const currentRecords = new Map(records.map((record) => [record.document_id, record]));
  const staleSelections = selected.filter((item) =>
    currentRecords.get(item.documentId)?.document_version_id !== item.documentVersionId,
  );

  // Count and newest collection time live in the frame footer instead of an inner header.
  useReportGadgetFreshness(
    records.reduce<string | null>((max, r) => (r.observed_at && (!max || r.observed_at > max) ? r.observed_at : max), null),
    records.length > 0 ? t('gadgetItemCount', { count: records.length }) : null,
  );

  /** Select-mode toggle; clears the selection on every flip. */
  const toggle = (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="h-6 px-1.5 text-xs"
      aria-pressed={selectMode}
      onClick={() => { setSelectMode((prev) => !prev); setSelectedRecords({}); }}
    >{selectMode ? t('tableSelectDone') : t('tableSelectMode')}</Button>
  );

  return (
    <section className="flex h-full flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      {/* At rest the toggle rides in the first row (no row of its own); selection mode needs room for Ask AI + Done. */}
      {records.length > 0 && selectMode && (
        <div className="flex items-center justify-end gap-2">
          {selectMode && (
            <Button
              type="button"
              size="sm"
              // Quiet outline at rest; primary only once rows are selected.
              variant={selected.length === 0 ? 'outline' : 'default'}
              disabled={selected.length === 0}
              onClick={() => openDrawer({ context: {
                kind: 'selection',
                items: selected.map(({ sourceId, documentId, documentVersionId }) => ({
                  sourceId, documentId, documentVersionId,
                })),
              } })}
            >
              {t('askAboutSelected', { count: selected.length })}
            </Button>
          )}
          {toggle}
        </div>
      )}
      {staleSelections.length > 0 && (
        <p role="status" className="text-xs text-muted-foreground">{t('selectionChanged', { count: staleSelections.length })}</p>
      )}
      {sourceIds.length === 0 ? <GadgetNotice kind="setup" className="p-0" message={t('gadgetDataEmpty')} />
        : query.isPending ? <GadgetSkeleton variant="table" className="p-0" label={t('gadgetDataLoading')} />
          : query.isError ? <GadgetNotice kind="error" className="p-0" message={t('gadgetDataLoadFailed')} />
            : records.length === 0 ? <GadgetNotice kind="empty" className="p-0" message={t('gadgetDataEmpty')} />
              : (
                // Stacked rows: title/excerpt on the left, date right on wide bodies and wrapped below on narrow ones.
                <ul className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto">
                  {records.map((record, index) => (
                    <TableRecordRow
                      key={record.document_id} record={record} selectMode={selectMode} checked={selectedIds.has(record.document_id)}
                      setSelectedRecords={setSelectedRecords} toggle={index === 0 && !selectMode ? toggle : null}
                    />
                  ))}
                </ul>
              )}
    </section>
  );
}
