'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchNewsTrends, fetchStories } from './story-api';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';
import { TranslatedBadge } from '@/modules/translations/translated-badge';
import { TranslationNote } from '@/modules/translations/translation-note';
import { useContentTranslation } from '@/modules/translations/use-content-translation';
import { safeHttpUrl } from '@/core/safe-url';
import type { Story } from './story-types';

/** Props for the source-scoped story gadget renderer. */
export type StoryListProps = {
  sourceIds: string[];
  topicId?: string;
  query?: string;
  limit?: number;
  showTrends?: boolean;
  onSelectStory: (storyId: string, trigger: HTMLButtonElement) => void;
};

/**
 * Render owner-authorized stories within fixed sources and bounded query/limit inputs.
 * A query is sent as the News API's all-token text filter; pagination retains its
 * API cursor and the selected source snapshot. Selection reports its trigger so the
 * owning dialog can restore focus; the gadget supplies a persistent fallback for
 * reconfiguration that removes the clicked button. Optional trends use the same bound
 * and can be disabled when their API cannot honor configured story filters.
 * @param props The immutable source selection, supported text query and item limit.
 * @returns A paged story list with optional matching-scope trends and detail selection.
 */
export function StoryList({
  sourceIds, topicId, query, limit = 25, showTrends = true, onSelectStory,
}: StoryListProps) {
  const t = useTranslations('news');
  const display = useDisplayPreferences();
  const incompleteLabel = useNewsIncompleteLabel();
  const storiesQuery = useInfiniteQuery({
    queryKey: ['news-stories', sourceIds, topicId, query, limit],
    queryFn: ({ pageParam }) => fetchStories(sourceIds, { cursor: pageParam, topicId, q: query, limit }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const trendQuery = useQuery({
    queryKey: ['news-trends', sourceIds, limit],
    queryFn: () => fetchNewsTrends(sourceIds, limit),
    staleTime: 30_000,
    enabled: showTrends,
  });
  // Failed refetches invalidate every cached page so stale excerpts do not survive authority errors.
  const stories = storiesQuery.isError ? [] : storiesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const trendItems = trendQuery.isError ? [] : trendQuery.data?.items ?? [];
  const incompleteReasons = new Set([
    ...(storiesQuery.isError ? [] : storiesQuery.data?.pages.flatMap((page) => page.incomplete_reasons) ?? []),
    ...(showTrends && !trendQuery.isError ? trendQuery.data?.incomplete_reasons ?? [] : []),
  ]);

  // Only the stories currently loaded are requested for translation.
  const targets = stories.filter((s) => s.translation_revision).map((s) => ({ id: s.id, revision: s.translation_revision as string }));
  const translation = useContentTranslation('news_story', targets);
  const [showOriginal, setShowOriginal] = useState(false);
  const shown = (story: Story) => (showOriginal ? undefined : translation.results.get(story.id)?.translation ?? undefined);

  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto rounded-lg border border-border bg-card p-3 text-card-foreground">
      <header className="flex items-center justify-between border-b border-border pb-2">
        <h2 className="text-sm font-semibold">{t('title')}</h2>
        {translation.results.size ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : null}
        <Button variant="ghost" size="icon" onClick={() => { void storiesQuery.refetch(); if (showTrends) void trendQuery.refetch(); }} aria-label={t('retry')}>
          <RefreshCw className={`size-4 ${storiesQuery.isFetching ? 'animate-spin' : ''}`} />
        </Button>
      </header>

      {showTrends && trendItems.length ? (
        <section aria-label={t('trends')} className="space-y-2">
          <h3 className="text-xs font-medium text-muted-foreground">{t('trends')}</h3>
          {trendItems.slice(0, 5).map((trend) => (
            <Button key={trend.story_id} type="button" variant="ghost" onClick={(event) => onSelectStory(trend.story_id, event.currentTarget)} className="h-auto min-h-11 w-full flex-col items-stretch justify-start whitespace-normal rounded-md border border-border p-2 text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <span className="block text-sm font-medium">{trend.title}</span>
              <span className="text-xs text-muted-foreground">
                {t('current24h')}: {trend.current_count} · {trend.low_baseline ? t('lowBaseline') : `${trend.ratio?.toFixed(1)}×`}
              </span>
            </Button>
          ))}
        </section>
      ) : null}

      {storiesQuery.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p> : null}
      {showTrends && trendQuery.isError ? <p role="alert" className="text-sm text-destructive">{t('error')}</p> : null}
      {incompleteReasons.size ? (
        <div role="status" className="space-y-1 text-xs text-muted-foreground">
          <p>{t('partialData')}</p>
          {[...incompleteReasons].map((reason) => <p key={reason}>{incompleteLabel(reason)}</p>)}
        </div>
      ) : null}
      {storiesQuery.isError ? (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm text-destructive">
          <span>{t('error')}</span>
          <Button variant="outline" size="sm" onClick={() => { void storiesQuery.refetch(); }}>{t('retry')}</Button>
        </div>
      ) : null}
      {!storiesQuery.isPending && !storiesQuery.isError && stories.length === 0 ? <p className="text-sm text-muted-foreground">{t('empty')}</p> : null}

      <ol className="space-y-2">
        {stories.map((story: Story) => (
          <li key={story.id}>
            <Button type="button" variant="ghost" onClick={(event) => onSelectStory(story.id, event.currentTarget)} className="h-auto min-h-11 w-full flex-col items-stretch justify-start whitespace-normal rounded-md border border-border p-3 text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <span className="flex items-start justify-between gap-2">
                <span className="font-medium leading-snug">{shown(story)?.title ?? story.title}</span>
                <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              </span>
              <span className="mt-1 line-clamp-3 block text-sm text-muted-foreground">{shown(story)?.excerpt ?? story.excerpt}</span>
              {!translation.results.has(story.id) && <TranslationNote issue={translation.issues.get(story.id)} />}
              <span className="mt-2 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                <span>{t('sources', { count: story.source_count })}</span>
                <span>{t('evidence', { count: story.evidence_count })}</span>
                <span>{formatDateTime(story.observed_at, display.locale, display.timezone)}</span>
                {story.relevance !== null ? <span>{t('relevance')}: {Math.round(story.relevance * 100)}%</span> : null}
              </span>
              {story.relevance_state === 'partial' ? <span className="mt-1 block text-xs text-muted-foreground">{t('unavailable')}</span> : null}
              {story.incomplete_reasons.map((reason) => (
                <span key={reason} className="mt-1 block text-xs text-muted-foreground">{incompleteLabel(reason)}</span>
              ))}
            </Button>
            <Attribution story={story} />
          </li>
        ))}
      </ol>
      {!storiesQuery.isError && storiesQuery.hasNextPage ? (
        <Button variant="outline" onClick={() => { void storiesQuery.fetchNextPage(); }} disabled={storiesQuery.isFetchingNextPage}>
          {storiesQuery.isFetchingNextPage ? t('loading') : t('loadMore')}
        </Button>
      ) : null}
    </section>
  );
}

/** Publisher and license beside a story; sits outside the selection button so the link is not nested interactive content. */
function Attribution({ story }: { story: Story }) {
  const t = useTranslations('news');
  const item = story.evidence?.find((e) => e.publisher);
  if (!item?.publisher) return null;
  const href = safeHttpUrl(item.url);
  return (
    <p className="mt-1 px-1 text-xs text-muted-foreground">
      {t('publisher')}: {href ? <a className="text-accent underline" href={href} target="_blank" rel="noopener noreferrer">{item.publisher}</a> : item.publisher}
      {item.license_label ? ` · ${item.license_label}` : ''}
    </p>
  );
}
