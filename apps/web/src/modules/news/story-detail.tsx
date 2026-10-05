'use client';

import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, ExternalLink } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchStory } from './story-api';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';

/** Props for the detail renderer shown when a story is opened from its gadget. */
export type StoryDetailProps = {
  storyId: string;
  sourceIds: string[];
  onBack: () => void;
};

/**
 * Render current excerpts and citations from one protected detail snapshot.
 * Ordinary title-free support pages retain their cursor; an authority-change
 * omission suppresses every cached page until the owner explicitly restarts.
 */
export function StoryDetail({ storyId, sourceIds, onBack }: StoryDetailProps) {
  const t = useTranslations('news');
  const display = useDisplayPreferences();
  const incompleteLabel = useNewsIncompleteLabel();
  const queryClient = useQueryClient();
  const [snapshotGeneration, setSnapshotGeneration] = useState(0);
  const detail = useInfiniteQuery({
    queryKey: ['news-story', storyId, sourceIds, snapshotGeneration],
    queryFn: ({ pageParam }) => fetchStory(storyId, sourceIds, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.evidence_cursor ?? undefined,
    enabled: Boolean(storyId),
    refetchOnMount: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
  /**
   * Drop every page from the prior cursor chain and start a fresh fixed-as-of read.
   * This is only invoked by an explicit retry/restart action, never automatically.
   */
  function restartSnapshot(): void {
    queryClient.removeQueries({ queryKey: ['news-story', storyId, sourceIds] });
    setSnapshotGeneration((generation) => generation + 1);
  }
  const pages = detail.data?.pages ?? [];
  const authorityChanged = pages.some((page) => page.incomplete_reasons.includes('evidence_changed_during_read'));
  const contentAvailable = !detail.isError && !authorityChanged;
  // A later authority failure revokes the whole cursor snapshot, including earlier cached pages.
  const visiblePages = contentAvailable ? pages : [];
  const story = visiblePages.find((page) => page.story !== null)?.story ?? undefined;
  const evidence = visiblePages.flatMap((page) => page.story?.evidence ?? []);
  const omissionReasons = [...new Set(visiblePages.flatMap((page) => page.incomplete_reasons))];

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto rounded-lg border border-border bg-card p-4 text-card-foreground">
      <Button variant="ghost" className="w-fit" onClick={onBack}>
        <ArrowLeft aria-hidden="true" className="mr-2 size-4" />{t('back')}
      </Button>
      {detail.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p> : null}
      {detail.isError || authorityChanged ? (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm text-destructive">
          <span>{authorityChanged ? t('evidenceChanged') : t('detailUnavailable')}</span>
          <Button variant="outline" size="sm" onClick={restartSnapshot}>{t('restartSnapshot')}</Button>
        </div>
      ) : null}
      {story ? (
        <article className="space-y-3">
          <h2 className="text-lg font-semibold leading-snug">{story.title}</h2>
          <p className="text-xs text-muted-foreground">
            {t('observed', { date: formatDateTime(story.observed_at, display.locale, display.timezone) })}
          </p>
          <h3 className="text-sm font-medium">{t('excerpt')}</h3>
          <blockquote className="border-l-2 border-border pl-3 text-sm leading-relaxed">{story.excerpt}</blockquote>
          {story.incomplete_reasons.length ? (
            <div role="status" className="space-y-1 text-xs text-muted-foreground">
              <p>{t('partialData')}</p>
              {story.incomplete_reasons.map((reason) => <p key={reason}>{incompleteLabel(reason)}</p>)}
            </div>
          ) : null}
          {story.relevance_signals.project?.available === false ? <p className="text-xs text-muted-foreground">{t('unavailable')}</p> : null}
        </article>
      ) : null}
      {contentAvailable && omissionReasons.length ? (
        <div role="status" className="space-y-1 text-xs text-muted-foreground">
          <p>{t('partialData')}</p>
          {omissionReasons.map((reason) => <p key={reason}>{incompleteLabel(reason)}</p>)}
        </div>
      ) : null}
      <section className="space-y-3" aria-label={t('evidence', { count: story?.evidence_count ?? 0 })}>
        {evidence.map((item) => (
          <article key={`${item.source_id}:${item.document_version_id}:${item.chunk_id}`} className="rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-medium">{item.title}</h3>
              {item.url ? (
                <a href={item.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-primary underline-offset-4 hover:underline">
                  {t('openSource')}<ExternalLink aria-hidden="true" className="size-3" />
                </a>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{item.source_name} · {formatDateTime(item.observed_at, display.locale, display.timezone)}</p>
            <p className="mt-2 text-sm">{item.excerpt}</p>
          </article>
        ))}
      </section>
      {contentAvailable && detail.hasNextPage ? (
        <Button variant="outline" onClick={() => { void detail.fetchNextPage(); }} disabled={detail.isFetchingNextPage}>
          {detail.isFetchingNextPage ? t('loading') : t('loadMore')}
        </Button>
      ) : null}
    </section>
  );
}
