'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { useQuery } from '@tanstack/react-query';
import { Badge } from '@/components/ui/badge';
import { listChatSourceNames } from './api';

/**
 * "Only from" line under a user message that was sent with `@`-picked sources. The message stores ids only, so the
 * names come from a separate, unfiltered source list; a stored id that no longer appears in it renders as an
 * "unavailable source" badge instead of a stale name.
 *
 * Renders nothing when the message has no scope. The name query lives in a child rendered only for a scoped message,
 * so ordinary messages never touch the query cache.
 *
 * @param props.sourceIds - Source ids the owner's `@` picker scoped the turn to (`MessageRead.source_scope`).
 */
export function MessageSourceScope({ sourceIds }: { sourceIds?: string[] }) {
  if (!sourceIds || sourceIds.length === 0) return null;
  return <ScopeNames sourceIds={sourceIds} />;
}

/**
 * Loads the unfiltered source names and renders one badge per scoped id. Shows nothing while the names are loading so
 * the badges never flash "unavailable" before they arrive; a failed load shows every id as unavailable.
 *
 * @param props.sourceIds - Non-empty list of scoped source ids, in the order they were picked.
 */
function ScopeNames({ sourceIds }: { sourceIds: string[] }) {
  const t = useTranslations('chat');
  const names = useQuery({
    queryKey: ['chat', 'source-names'],
    queryFn: ({ signal }) => listChatSourceNames(signal),
    staleTime: 60_000,
  });
  if (names.isPending) return null;
  const nameById = new Map((names.data ?? []).map((source) => [source.id, source.name]));
  return (
    <div className="flex flex-wrap items-center justify-end gap-1.5 text-xs" data-testid="message-source-scope">
      <span className="text-muted-foreground">{t('sourceScope.label')}</span>
      {sourceIds.map((id) => (
        <Badge key={id} variant="secondary" className="max-w-full">
          <span className="truncate">{nameById.get(id) ?? t('sourceScope.unknownSource')}</span>
        </Badge>
      ))}
    </div>
  );
}
