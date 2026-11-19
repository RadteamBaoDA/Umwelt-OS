'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { TitleBarSlot } from '@/components/gadget-title-slot';
import { useChatController } from '@/core/app-shell/chat-controller';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useSkin } from '@/core/use-skin';
import { cleanExcerpt } from '@/modules/news';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';
import {
  CollectionTranslationBar,
  ItemTranslateButton,
  TranslationStatusBanner,
  useItemTranslation,
  type ItemTranslationController,
} from '@/modules/translations';
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

/** Literal catalog keys per record severity badge (no runtime key concatenation). */
const SEVERITY_KEY = {
  low: 'severityLevel.severityLow',
  medium: 'severityLevel.severityMedium',
  high: 'severityLevel.severityHigh',
  critical: 'severityLevel.severityCritical',
} as const;

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

/**
 * Compact terminal timestamp (`Nov 10 19:20`): short month, day and 24-hour clock in the owner's timezone, no year.
 *
 * @param value ISO instant; an unparsable value renders as an empty string.
 * @param locale Display locale id.
 * @param timeZone IANA timezone of the owner's display preference.
 */
function formatTerminalStamp(value: string | null | undefined, locale: string, timeZone: string): string {
  const at = value ? new Date(value) : null;
  if (!at || Number.isNaN(at.getTime())) return '';
  return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone })
    .format(at)
    .replace(',', '');
}

/** Select-mode toggle button; the caller owns the state and clears the selection in `onToggle`. */
function SelectModeToggle({ selectMode, onToggle }: { selectMode: boolean; onToggle: () => void }) {
  const t = useTranslations('dashboard');
  return (
    <Button type="button" size="sm" variant="ghost" className="h-6 px-1.5 text-xs" aria-pressed={selectMode} onClick={onToggle}>
      {selectMode ? t('tableSelectDone') : t('tableSelectMode')}
    </Button>
  );
}

/** Terminal-only: hosts the select toggle in the gadget title bar (it rides in the first row otherwise). */
function TableToolbar({ visible, toggle }: { visible: boolean; toggle: React.ReactNode }) {
  if (useSkin() !== 'terminal' || !visible) return null;
  return <TitleBarSlot>{toggle}</TitleBarSlot>;
}

/** Terminal-only column header row (TITLE / SOURCE / PUBLISHED); decorative, so hidden from assistive technology. */
function TableHead() {
  const t = useTranslations('dashboard');
  if (useSkin() !== 'terminal') return null;
  return (
    <li className="tbl-head" aria-hidden="true">
      <span>{t('tableColTitle')}</span>
      <span>{t('tableColSource')}</span>
      <span>{t('tableColPublished')}</span>
    </li>
  );
}

/** One stacked record row: optional selection checkbox, title with severity badge, distinct excerpt and date. */
/** Checkbox toggling one document version in the bounded 32-item selection map. */
function TableRowCheckbox({
  record,
  checked,
  setSelectedRecords,
}: {
  record: FeedDocument;
  checked: boolean;
  setSelectedRecords: React.Dispatch<React.SetStateAction<Record<string, SelectedRecordIdentity>>>;
}) {
  const t = useTranslations('dashboard');
  return (
    <Checkbox
      aria-label={t('selectRecord', { title: record.title })}
      checked={checked}
      onCheckedChange={(next) =>
        setSelectedRecords((current) => {
          if (next !== true) {
            const remaining = { ...current };
            delete remaining[record.document_id];
            return remaining;
          }
          if (Object.keys(current).length >= 32) return current;
          return {
            ...current,
            [record.document_id]: {
              sourceId: record.source_id,
              documentId: record.document_id,
              documentVersionId: record.document_version_id,
              versionNumber: record.version_number,
            },
          };
        })
      }
    />
  );
}

/** Title, optional severity badge, and distinct excerpt column of a table row. */
function TableRowTitle({ title, excerpt }: { title: string; excerpt: string | null }) {
  const t = useTranslations('dashboard');
  const shown = splitSeverity(title);
  return (
    <div className="min-w-0 flex-1 basis-48">
      <p className="text-sm font-medium leading-snug">
        {shown.title}
        {shown.severity && (
          <Badge variant="outline" className="ml-2 align-middle">
            {t(SEVERITY_KEY[shown.severity])}
          </Badge>
        )}
      </p>
      {excerpt && <p className="tbl-excerpt mt-0.5 line-clamp-2 text-xs text-muted-foreground">{excerpt}</p>}
    </div>
  );
}

/** One stacked record row: optional checkbox, title/severity/excerpt, optional source, translate toggle and date. */
function TableRecordRow({
  record,
  selectMode,
  checked,
  setSelectedRecords,
  toggle,
  translation,
}: {
  record: FeedDocument;
  selectMode: boolean;
  checked: boolean;
  setSelectedRecords: React.Dispatch<React.SetStateAction<Record<string, SelectedRecordIdentity>>>;
  /** The select-mode toggle, rendered in the first row at rest. */
  toggle: React.ReactNode;
  /** Shared on-demand document translation controller for this table. */
  translation: ItemTranslationController;
}) {
  const display = useDisplayPreferences();
  const terminal = useSkin() === 'terminal';
  const translated = translation.shown(record.document_id);
  const effectiveTitle = translated?.title ?? record.title;
  const sourceName = record.provider_metadata?.source_fields?.source;
  const excerpt = distinctExcerpt(effectiveTitle, translated?.excerpt ?? record.excerpt);
  const stamp = record.published_at ?? record.observed_at;
  return (
    <li className="tbl-row flex flex-wrap items-start gap-x-3 gap-y-0.5 py-2">
      {selectMode && <TableRowCheckbox record={record} checked={checked} setSelectedRecords={setSelectedRecords} />}
      <TableRowTitle title={effectiveTitle} excerpt={excerpt} />
      {terminal && <span className="tbl-src">{typeof sourceName === 'string' ? sourceName : ''}</span>}
      <div className="tbl-date flex shrink-0 flex-col items-end gap-0.5">
        {terminal ? null : toggle}
        <ItemTranslateButton controller={translation} target={{ id: record.document_id, revision: record.document_version_id }} />
        <time className="whitespace-nowrap text-xs tabular-nums text-muted-foreground" dateTime={stamp ?? undefined}>
          {terminal
            ? formatTerminalStamp(stamp, display.locale, display.timezone)
            : formatDateTime(stamp, display.locale, display.timezone)}
        </time>
      </div>
    </li>
  );
}

/** Selection-mode Ask AI + Done action row for `TablePanel`. */
function TableSelectionBar({
  selected,
  toggle,
  onAsk,
}: {
  selected: SelectedRecordIdentity[];
  toggle: React.ReactNode;
  onAsk: () => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className="flex items-center justify-end gap-2">
      <Button
        type="button"
        size="sm"
        variant={selected.length === 0 ? 'outline' : 'default'}
        disabled={selected.length === 0}
        onClick={onAsk}
      >
        {t('askAbout.selected', { count: selected.length })}
      </Button>
      {toggle}
    </div>
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
  const translationTargets = useMemo(
    () => records.map((record) => ({ id: record.document_id, revision: record.document_version_id })),
    [records],
  );
  const translation = useItemTranslation('document', translationTargets);
  const selected = Object.values(selectedRecords);
  const selectedIds = new Set(Object.keys(selectedRecords));
  const currentRecords = new Map(records.map((record) => [record.document_id, record]));
  const staleSelections = selected.filter((item) => currentRecords.get(item.documentId)?.document_version_id !== item.documentVersionId);
  // Count and newest collection time live in the frame footer instead of an inner header.
  useReportGadgetFreshness(
    records.reduce<string | null>((max, r) => (r.observed_at && (!max || r.observed_at > max) ? r.observed_at : max), null),
    records.length > 0 ? t('gadget.itemCount', { count: records.length }) : null,
  );
  /** Select-mode toggle; clears the selection on every flip. */
  const toggle = (
    <SelectModeToggle
      selectMode={selectMode}
      onToggle={() => {
        setSelectMode((prev) => !prev);
        setSelectedRecords({});
      }}
    />
  );

  return (
    <section className="flex h-full flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      {/* At rest the toggle rides in the first row (no row of its own); selection mode needs room for Ask AI + Done. */}
      <TableToolbar visible={records.length > 0 && !selectMode} toggle={toggle} />
      {records.length > 0 && selectMode && (
        <TableSelectionBar
          selected={selected}
          toggle={toggle}
          onAsk={() =>
            openDrawer({
              context: {
                kind: 'selection',
                items: selected.map(({ sourceId, documentId, documentVersionId }) => ({ sourceId, documentId, documentVersionId })),
              },
            })
          }
        />
      )}
      {staleSelections.length > 0 && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('selectionChanged', { count: staleSelections.length })}
        </p>
      )}
      {records.length > 0 ? <CollectionTranslationBar controller={translation} /> : null}
      <TranslationStatusBanner issues={translation.issues} />
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
      ) : records.length === 0 ? (
        <GadgetNotice kind="empty" className="p-0" message={t('gadget.dataEmpty')} />
      ) : (
        // Stacked rows: title/excerpt on the left, date right on wide bodies and wrapped below on narrow ones.
        <ul className="tbl-list min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto">
          <TableHead />
          {records.map((record, index) => (
            <TableRecordRow
              key={record.document_id}
              record={record}
              selectMode={selectMode}
              checked={selectedIds.has(record.document_id)}
              setSelectedRecords={setSelectedRecords}
              toggle={index === 0 && !selectMode ? toggle : null}
              translation={translation}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
