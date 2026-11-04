'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import Link from 'next/link';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchNewsTrends, fetchStories } from './story-api';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';
import { TranslatedBadge } from '@/modules/translations/translated-badge';
import { TranslationNote } from '@/modules/translations/translation-note';
import { useContentTranslation } from '@/modules/translations/use-content-translation';
import { safeHttpUrl } from '@/core/safe-url';
import { cleanExcerpt } from './excerpt';
import { formatRelativeTime } from '@/modules/dashboard/gadgets/relative-time';
import type { Story } from './story-types';

/** Props for the source-scoped story gadget renderer. */
export type StoryListProps = {
  sourceIds: string[];
  topicId?: string;
  query?: string;
  limit?: number;
  showTrends?: boolean;
  /** Rendered inside a dashboard gadget frame: no own title row, card border or per-story cards. */
  embedded?: boolean;
  /** Page mode: element (e.g. in the page title row) that receives the translated badge and refresh button instead of an own header. */
  actionsHost?: HTMLElement | null;
  /** True when the owner has no active sources: the empty state then points to source setup instead of a refresh. */
  noSources?: boolean;
  onSelectStory: (storyId: string, trigger: HTMLButtonElement) => void;
};

/**
 * Render owner-authorized stories within fixed sources and bounded query/limit inputs.
 * A query is sent as the News API's all-token text filter; pagination retains its
 * API cursor and the selected source snapshot. Selection reports its trigger so the
 * owning dialog can restore focus; the gadget supplies a persistent fallback for
 * reconfiguration that removes the clicked button. Optional trends use the same bound
 * and can be disabled when their API cannot honor configured story filters. `embedded`
 * drops the page-level title row and card chrome so the host gadget frame is the only header.
 * @param props The immutable source selection, supported text query and item limit.
 * @returns A paged story list with optional matching-scope trends and detail selection.
 */
export function StoryList({
  sourceIds, topicId, query, limit = 25, showTrends = true, embedded = false, actionsHost, noSources = false, onSelectStory,
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
  /** Translated title/excerpt for a story, or undefined when showing the original or no translation exists. */
  const shown = (story: Story) => (showOriginal ? undefined : translation.results.get(story.id)?.translation ?? undefined);

  // Embedded gadgets show the partial-data notice in the slim top meta row.
  const incompleteNotice = incompleteReasons.size ? (
    <p role="status" className="text-xs text-muted-foreground" title={[...incompleteReasons].map(incompleteLabel).join(' ') || undefined}>{t('incompleteNotice')}</p>
  ) : null;

  // A relevance score identical on every visible story carries no ranking signal, so it is hidden.
  // Compare the rounded percentage actually displayed: 0.331 and 0.334 both read "33%".
  // Near-flat scores (spread under 10 percentage points) are noise too, so require a visible spread.
  const scores = stories.flatMap((s) => (s.relevance == null ? [] : [Math.round(s.relevance * 100)]));
  const relevanceVaries = scores.length > 1 && Math.max(...scores) - Math.min(...scores) >= 10;

  // Embedded lead: the story with the most sources (first on ties) gets the hero treatment.
  const leadStory = embedded && stories.length > 0
    ? stories.reduce((best, story) => (story.source_count > best.source_count ? story : best), stories[0])
    : null;
  const listStories = leadStory ? stories.filter((story) => story.id !== leadStory.id) : stories;

  const refreshButton = (
    <Button variant="ghost" size="icon" className={embedded ? 'size-6' : undefined} onClick={() => { void storiesQuery.refetch(); if (showTrends) void trendQuery.refetch(); }} aria-label={t('retry')}>
      <RefreshCw className={`${embedded ? 'size-3.5' : 'size-4'} ${storiesQuery.isFetching ? 'animate-spin' : ''}`} />
    </Button>
  );

  const pageActions: ReactNode = (
    <>
      {translation.results.size ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : null}
      {refreshButton}
    </>
  );

  /** Relative time ("2 hr. ago") for list meta; the full datetime lives in the element title. */
  const relativeTime = (iso: string) => formatRelativeTime(iso, normalizeFormattingLocale(display.locale)) || formatDateTime(iso, display.locale, display.timezone);

  /** One compact story row: title, excerpt, count/time meta and publisher attribution. */
  const renderStory = (story: Story) => (
        <li key={story.id}>
            <Button type="button" variant="ghost" onClick={(event) => onSelectStory(story.id, event.currentTarget)} className={`h-auto w-full flex-col items-stretch justify-start whitespace-normal text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-none px-0 py-3`}>
              <span className="flex items-start justify-between gap-2">
                <span className="font-medium leading-snug">{shown(story)?.title ?? story.title}</span>
                <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              </span>
              {cleanExcerpt(shown(story)?.title ?? story.title, shown(story)?.excerpt ?? story.excerpt) ? (
                <span className="mt-1 line-clamp-3 text-sm text-muted-foreground">{cleanExcerpt(shown(story)?.title ?? story.title, shown(story)?.excerpt ?? story.excerpt)}</span>
              ) : null}
              {!translation.results.has(story.id) && <TranslationNote issue={translation.issues.get(story.id)} />}
              <span className="mt-2 block text-xs text-muted-foreground" title={formatDateTime(story.observed_at, display.locale, display.timezone)}>
                {[
                  // A lone source with a lone item carries no information, so only larger counts are shown.
                  ...(story.source_count === 1 && story.evidence_count === 1 ? [] : [
                    t('sources', { count: story.source_count }),
                    t('evidence', { count: story.evidence_count }),
                  ]),
                  relativeTime(story.observed_at),
                ].join(' · ')}
                {story.relevance !== null && relevanceVaries ? ` · ${t('relevance')} ${Math.round(story.relevance * 100)}%` : null}
              </span>
              {story.incomplete_reasons.map((reason) => (
                <span key={reason} className="mt-1 block text-xs text-muted-foreground">{incompleteLabel(reason)}</span>
              ))}
            </Button>
            <Attribution story={story} />
        </li>
  );

  return (
    <section className={embedded
      ? 'flex h-full min-h-0 flex-col gap-2 overflow-auto bg-card px-3 pb-3 text-card-foreground'
      : 'flex flex-col gap-3 bg-card text-card-foreground'}>
      {embedded ? (
        // Slim meta row: partial-data notice on the left, translated badge + refresh on the right (no overlap with stories).
        <div className="flex h-6 shrink-0 items-center justify-between gap-2">
          <div className="min-w-0 truncate">{incompleteNotice}</div>
          <div className="flex items-center gap-1">
            {translation.results.size ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : null}
            {refreshButton}
          </div>
        </div>
      ) : actionsHost ? createPortal(pageActions, actionsHost) : (
        <header className="flex items-center justify-end gap-1 border-b border-border pb-2">{pageActions}</header>
      )}

      {showTrends && trendItems.length && embedded ? (
        // Embedded trends are a one-line chip strip (title + ratio) so stories stay the focus.
        <section aria-label={t('trends')} className="flex shrink-0 gap-2 overflow-x-auto pb-1">
          <h3 className="sr-only">{t('trends')}</h3>
          {trendItems.slice(0, 5).map((trend) => (
            <Button key={trend.story_id} type="button" variant="outline" size="sm" title={`${t('current24h')}: ${trend.current_count}`} onClick={(event) => onSelectStory(trend.story_id, event.currentTarget)} className="h-7 max-w-56 shrink-0 gap-1.5 rounded-full px-3 text-xs font-normal">
              <span className="truncate">{trend.title}</span>
              <span className="tabular-nums text-muted-foreground">{trend.low_baseline ? t('lowBaseline') : `${trend.ratio?.toFixed(1)}×`}</span>
            </Button>
          ))}
        </section>
      ) : showTrends && trendItems.length ? (
        <section aria-label={t('trends')} className="space-y-2">
          <h3 className="text-xs font-medium text-muted-foreground">{t('trends')}</h3>
          {trendItems.slice(0, 5).map((trend) => (
            <Button key={trend.story_id} type="button" variant="ghost" onClick={(event) => onSelectStory(trend.story_id, event.currentTarget)} className="h-auto w-full flex-col items-stretch justify-start whitespace-normal rounded-md border border-border p-2 text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
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
      {!embedded ? incompleteNotice : null}
      {storiesQuery.isError ? (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm text-destructive">
          <span>{t('error')}</span>
          <Button variant="outline" size="sm" onClick={() => { void storiesQuery.refetch(); }}>{t('retry')}</Button>
        </div>
      ) : null}
      {!storiesQuery.isPending && !storiesQuery.isError && stories.length === 0 ? (
        <div className="flex flex-col items-start gap-2 text-sm text-muted-foreground" data-testid="news-empty">
          {noSources ? (
            <>
              <p className="m-0">{t('emptyNoSources')}</p>
              <Button asChild variant="outline" size="sm"><Link href="/settings/sources">{t('addNewsSource')}</Link></Button>
            </>
          ) : (
            <>
              <p className="m-0">{t('emptyAwaitingCollection')}</p>
              <Button variant="outline" size="sm" onClick={() => { void storiesQuery.refetch(); }}>{t('refresh')}</Button>
            </>
          )}
        </div>
      ) : null}

      {leadStory ? (
        // Wide gadgets put the lead left and the list right; narrow ones stack them.
        <div className="grid shrink-0 gap-x-6 gap-y-2 @[40rem]/gadget:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <div data-testid="news-lead" className="min-w-0">
            <Button type="button" variant="ghost" onClick={(event) => onSelectStory(leadStory.id, event.currentTarget)} className="h-auto w-full flex-col items-stretch justify-start whitespace-normal rounded-none px-0 py-2 text-left font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <span className="text-xl font-semibold leading-snug">{shown(leadStory)?.title ?? leadStory.title}</span>
              {cleanExcerpt(shown(leadStory)?.title ?? leadStory.title, shown(leadStory)?.excerpt ?? leadStory.excerpt) ? (
                <span className="mt-2 line-clamp-3 text-sm leading-relaxed text-muted-foreground">{cleanExcerpt(shown(leadStory)?.title ?? leadStory.title, shown(leadStory)?.excerpt ?? leadStory.excerpt)}</span>
              ) : null}
              <span className="mt-3 flex items-baseline gap-1.5 text-xs text-muted-foreground">
                {/* A single source is not a headline figure: show it as plain meta text instead. */}
                {leadStory.source_count >= 2 ? (
                  <>
                    <span className="text-figure-sm text-foreground">{leadStory.source_count}</span>
                    <span>{t('sourcesLabel', { count: leadStory.source_count })}</span>
                  </>
                ) : <span>{t('sources', { count: leadStory.source_count })}</span>}
                <span aria-hidden="true">·</span>
                <span className="tabular-nums" title={formatDateTime(leadStory.observed_at, display.locale, display.timezone)}>{relativeTime(leadStory.observed_at)}</span>
              </span>
            </Button>
            <Attribution story={leadStory} />
          </div>
          <ol className="min-w-0 divide-y divide-border">{listStories.map(renderStory)}</ol>
        </div>
      ) : (
        <ol className="shrink-0 divide-y divide-border">{listStories.map(renderStory)}</ol>
      )}
      {!storiesQuery.isError && storiesQuery.hasNextPage ? (
        <Button variant="outline" className="mt-1 shrink-0" onClick={() => { void storiesQuery.fetchNextPage(); }} disabled={storiesQuery.isFetchingNextPage}>
          {storiesQuery.isFetchingNextPage ? t('loading') : t('loadMore')}
        </Button>
      ) : null}
    </section>
  );
}

/** Publisher name beside a story (licence text kept in the link/name title for compliance); sits outside the selection button so the link is not nested interactive content. */
function Attribution({ story }: { story: Story }) {
  const t = useTranslations('news');
  const item = story.evidence?.find((e) => e.publisher);
  if (!item?.publisher) return null;
  const href = safeHttpUrl(item.url);
  return (
    <p className="-mt-2 pb-3 text-xs text-muted-foreground" title={item.license_label ?? undefined}>
      {t('publisher')}: {href ? <a className="text-foreground underline underline-offset-2" href={href} target="_blank" rel="noopener noreferrer" title={item.license_label ?? undefined}>{item.publisher}</a> : item.publisher}
    </p>
  );
}
