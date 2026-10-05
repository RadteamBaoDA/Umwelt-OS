'use client';

import { FileText, Sparkles } from 'lucide-react';
import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import type { GadgetInstance } from '../api';
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
      <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
        <div className="flex items-center gap-1.5 font-medium">
          <FileText className="w-3.5 h-3.5 text-primary" />
          <span>{t('textPanelTitle')}</span>
        </div>
        <span className="font-mono text-[10px]">{t('gadgetRevision', { revision: definition.revision })}</span>
      </div>

      {content ? (
        <div className="text-xs leading-relaxed text-foreground/90 whitespace-pre-wrap font-sans">{content}</div>
      ) : sourceIds.length === 0 ? <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataEmpty')}</p>
        : query.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataLoading')}</p>
        : query.isError ? <p role="alert" className="text-sm text-destructive">{t('gadgetDataLoadFailed')}</p>
          : query.data?.items.length ? query.data.items.slice(0, 5).map((item) => (
            <section key={item.document_version_id} className="space-y-1">
              <h3 className="text-xs font-semibold">{item.title}</h3>
              <p className="text-xs leading-relaxed text-foreground/90 whitespace-pre-wrap">{item.excerpt}</p>
            </section>
          )) : <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataEmpty')}</p>}
    </div>
  );
}
