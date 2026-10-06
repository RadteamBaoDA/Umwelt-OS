'use client';

import Link from 'next/link';
import { useCallback, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { DetailDialog, type DetailTarget } from '@/modules/detail/detail-dialog';
import { StoryList } from '@/modules/news/story-list';
import type { GadgetInstance } from '../api';

/** Props for the source-scoped News story gadget. */
export interface NewsFeedProps {
  /** Saved source and filter configuration for this dashboard gadget instance. */
  instance: GadgetInstance;
}

/**
 * Render configured News stories and open the selected story in an accessible dialog.
 * An empty configured source selection clears the current selection and produces a setup
 * notice without mounting any request-owning story components. Source IDs are snapshotted
 * on selection so list and detail use the same immutable scope; dialog closure restores
 * focus to its trigger or
 * the persistent gadget fallback if reconfiguration removed that trigger.
 * The existing owner API applies the fixed source IDs, an all-token keyword query of at
 * most 200 characters, and an item limit of at most 100. Unsupported source-item selectors,
 * excluded keywords and highlights are surfaced instead of silently treated as applied.
 * @param props The dashboard gadget instance and its saved configuration.
 * @returns The configured News list and optional detail dialog.
 */
export function NewsFeed({ instance }: NewsFeedProps) {
  const t = useTranslations('news');
  const [selectedTarget, setSelected] = useState<DetailTarget | null>(null);
  const returnFocusRef = useRef<HTMLButtonElement>(null);
  const listFallbackRef = useRef<HTMLDivElement>(null);
  // Snapshot the configured scope once per definition change; the detail dialog cannot widen it.
  const sourceIds = useMemo(() => [...instance.definition.source_ids], [instance.definition.source_ids]);
  // An unconfigured gadget never shows a stale selection.
  const selected = sourceIds.length > 0 ? selectedTarget : null;
  const closeDetail = useCallback(() => setSelected(null), []);
  const { filters, scope, highlight_rules: highlightRules } = instance.definition;
  const keywordQuery = (filters.keywords ?? []).map((word) => word.trim()).filter(Boolean).join(' ');
  const queryIsTooLong = keywordQuery.length > 200;
  const query = keywordQuery && !queryIsTooLong ? keywordQuery : undefined;
  const hasUnsupportedScope = Object.values(scope).some((values) => Array.isArray(values) && values.length > 0);
  const hasUnsupportedExclusions = Boolean(filters.exclude_keywords?.length);
  const hasUnsupportedHighlights = highlightRules.length > 0;
  const unsupportedNotices = [
    ...(hasUnsupportedScope ? [t('unsupportedScopeSelectors')] : []),
    ...(hasUnsupportedExclusions ? [t('unsupportedExcludeKeywords')] : []),
    ...(hasUnsupportedHighlights ? [t('unsupportedHighlights')] : []),
    ...(queryIsTooLong ? [t('unsupportedKeywordLimit')] : []),
    ...(keywordQuery ? [t('trendsFilterUnavailable')] : []),
  ];
  const showTrends = unsupportedNotices.length === 0;

  return (
    <div ref={listFallbackRef} tabIndex={-1} className="flex h-full min-h-0 flex-col gap-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
      {unsupportedNotices.length ? (
        <div role="status" className="space-y-1 rounded-md border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
          {unsupportedNotices.map((notice) => <p key={notice}>{notice}</p>)}
        </div>
      ) : null}
      {sourceIds.length === 0 ? (
        <div role="status" className="rounded-md border border-border bg-muted/20 p-3 text-sm text-muted-foreground">
          <p>{t('sourceSelectionRequired')}</p>
          <Link href="/settings/sources" className="mt-2 inline-flex min-h-11 items-center text-primary underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {t('configureSources')}
          </Link>
        </div>
      ) : (
        <StoryList
          sourceIds={sourceIds}
          query={query}
          limit={filters.limit ?? 25}
          showTrends={showTrends}
          onSelectStory={(storyId, trigger) => {
            returnFocusRef.current = trigger;
            setSelected({ kind: 'story', id: storyId, sourceGadget: instance.id, sourceIds: [...sourceIds] });
          }}
        />
      )}
      <DetailDialog
        target={selected}
        onClose={closeDetail}
        onCloseAutoFocus={(event) => {
          // The controlled dialog has no DialogTrigger, so return focus to its actual opener.
          event.preventDefault();
          const trigger = returnFocusRef.current;
          if (trigger?.isConnected) trigger.focus();
          else listFallbackRef.current?.focus();
          returnFocusRef.current = null;
        }}
      />
    </div>
  );
}
