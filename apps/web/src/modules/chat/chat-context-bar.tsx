'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { FileTextIcon, XIcon } from 'lucide-react';
import type { ChatContext } from '@/modules/chat/api';

/** Builds the human-readable label for the grounding context attached to the active chat. */
export function useContextLabel(context: ChatContext | null) {
  const t = useTranslations('chat');
  if (!context) return null;
  if (context.kind === 'day') return t('contextDay', { date: String(context.date ?? ''), timezone: String(context.timezone ?? '') });
  if (context.kind === 'document') return t('contextDocument', { title: String(context.resource_id ?? '') });
  if (context.kind === 'entity') return t('contextEntity', { name: String(context.resource_id ?? '') });
  return t('contextSelection', { count: context.items?.length ?? 1 });
}

/** Removable context chip row shared by the quick-chat drawer and the full Chat page. */
export function ChatContextBar({ context, onRemove, showAddNote = false }: { context: ChatContext | null; onRemove: () => void; showAddNote?: boolean }) {
  const t = useTranslations('chat');
  const label = useContextLabel(context);
  if (!context || !label) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-secondary px-4 py-2 text-xs text-foreground">
      <span className="sr-only">{t('contextLabel')}</span>
      <span className="inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-full border border-border bg-background py-0.5 pl-2.5 pr-1">
        <FileTextIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="truncate">{label}</span>
        <button
          type="button"
          onClick={onRemove}
          className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          title={t('contextRemove', { label })}
          aria-label={t('contextRemove', { label })}
        >
          <XIcon className="size-3.5" aria-hidden="true" />
        </button>
      </span>
      {showAddNote && <span className="text-muted-foreground">{t('addContextUnavailable')}</span>}
    </div>
  );
}
