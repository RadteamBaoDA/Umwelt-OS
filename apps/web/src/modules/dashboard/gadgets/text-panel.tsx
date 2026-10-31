'use client';

import { FileText, Sparkles } from 'lucide-react';
import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { cleanExcerpt } from '@/modules/news/excerpt';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge/api';

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

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3.5 space-y-3 overflow-y-auto">
      {/* Revision 1 is the default and says nothing; show the header only for edited gadgets. */}
      {definition.revision > 1 ? (
        <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
          <FileText className="w-3.5 h-3.5 text-muted-foreground" aria-hidden="true" />
          <span className="tabular-nums">{t('gadgetRevision', { revision: definition.revision })}</span>
        </div>
      ) : null}

      {content ? (
        <div className="text-xs leading-relaxed text-foreground/90 whitespace-pre-wrap font-sans">{content}</div>
      ) : sourceIds.length === 0 ? <GadgetNotice kind="setup" className="p-0" message={t('gadgetDataEmpty')} />
        : query.isPending ? <GadgetSkeleton variant="list" className="p-0" label={t('gadgetDataLoading')} />
        : query.isError ? <GadgetNotice kind="error" className="p-0" message={t('gadgetDataLoadFailed')} />
          : query.data?.items.length ? query.data.items.slice(0, 5).map((item) => (
            <section key={item.document_version_id} className="space-y-1">
              <h3 className="text-xs font-semibold">{item.title}</h3>
              <p className="text-xs leading-relaxed text-foreground/90 whitespace-pre-wrap">{cleanExcerpt(item.title, item.excerpt)}</p>
            </section>
          )) : <GadgetNotice kind="empty" className="p-0" message={t('gadgetDataEmpty')} />}
    </div>
  );
}
