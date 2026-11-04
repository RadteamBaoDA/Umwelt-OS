'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { cleanExcerpt } from '@/modules/news';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';

/** Props for the TextPanel gadget component. */
export interface TextPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional markdown or plain text content. */
  content?: string;
}

/**
 * Standard Text Panel gadget template.
 * Displays formatted notes, intelligence memos, or document summaries with sanitized text rendering.
 *
 * @param props Gadget instance configuration and optional text content.
 * @returns Accessible text panel component.
 */
export function TextPanel({ instance, content }: TextPanelProps) {
  const definition = instance.definition;
  const t = useTranslations('dashboard');
  const sourceIds = [...new Set(definition.source_ids)].slice(0, 32);
  const query = useQuery({
    queryKey: [...documentKeys.all, 'text-panel', sourceIds],
    enabled: sourceIds.length > 0 && !content,
    queryFn: () => listGadgetDocumentProjections(sourceIds),
  });

  // Revision 1 says nothing; an edited gadget reports its revision in the frame footer instead of an inner header.
  const newestObserved = query.data?.items.reduce<string | null>((max, item) => (item.observed_at && (!max || item.observed_at > max) ? item.observed_at : max), null) ?? null;
  useReportGadgetFreshness(newestObserved, definition.revision > 1 ? t('gadgetRevision', { revision: definition.revision }) : null);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3.5 space-y-3 overflow-y-auto">
      {content ? (
        <div className="max-w-prose whitespace-pre-wrap font-sans text-sm leading-[1.6] text-foreground">{content}</div>
      ) : sourceIds.length === 0 ? <GadgetNotice kind="setup" className="p-0" message={t('gadgetDataEmpty')} />
        : query.isPending ? <GadgetSkeleton variant="list" className="p-0" label={t('gadgetDataLoading')} />
        : query.isError ? <GadgetNotice kind="error" className="p-0" message={t('gadgetDataLoadFailed')} />
          : query.data?.items.length ? query.data.items.slice(0, 5).map((item, index) => (
            <section key={item.document_version_id} className="max-w-prose space-y-1">
              {/* The first document leads at 17px; the rest are plain 14px headings. */}
              <h3 className={index === 0 ? 'text-[17px] font-semibold leading-snug' : 'text-sm font-semibold'}>{item.title}</h3>
              <p className="whitespace-pre-wrap text-sm leading-[1.6] text-foreground">{cleanExcerpt(item.title, item.excerpt)}</p>
            </section>
          )) : <GadgetNotice kind="empty" className="p-0" message={t('gadgetDataEmpty')} />}
    </div>
  );
}
