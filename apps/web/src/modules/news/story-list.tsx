'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import Link from 'next/link';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { FeedImage } from '@/components/feed-image';
import { FeedSourceTabs } from '@/components/feed-source-tabs';
import { FEED_VIEWS, FeedViewToggle, type FeedView } from '@/components/feed-view-toggle';
import { ShowMoreButton, useShowMore } from '@/components/show-more';
import { Button } from '@/components/ui/button';
import { useLocalPref } from '@/core/use-local-pref';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchNewsTrends, fetchStories } from './story-api';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';
import { TranslatedBadge } from '@/modules/translations';
import { TranslationNote } from '@/modules/translations';
import { useContentTranslation } from '@/modules/translations';
import { safeHttpUrl } from '@/core/safe-url';
import { cleanExcerpt } from './excerpt';
import { formatRelativeTime } from '@/modules/dashboard/public-api';
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
  /** Dashboard gadget instance id: scopes the persisted List/Cards preference (embedded mode). */
  instanceId?: string;
  /** Resolved names of `sourceIds` for the source tabs (embedded mode, 2-6 sources). */
  sourceNames?: { id: string; name: string }[];
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
export function StoryList(props: StoryListProps) {
  const s0 = useStoryListData(props);
  const { t, storiesQuery, embedded } = s0;

  const s1 = useStoryListPresentation(s0);

  const s2 = buildStoryListActions(s1);

  const s3 = buildStoryRenderers(s2);

  const vm: StoryListVm = s3;
  return (
    <section
      className={
        embedded
          ? 'flex h-full min-h-0 flex-col gap-2 overflow-auto bg-card px-3 pb-3 text-card-foreground'
          : 'flex flex-col gap-3 bg-card text-card-foreground'
      }
    >
      <StoryListChrome vm={vm} />

      {embedded ? <FeedSourceTabs sourceIds={vm.sourceIds} sourceNames={vm.sourceNames} value={vm.sourceTab} onChange={vm.setSourceTab} /> : null}

      <StoryTrends vm={vm} />

      <StoryListStatus vm={vm} />

      <StoryListBody vm={vm} />
      {embedded ? <ShowMoreButton state={vm.more} /> : null}
      {/* Embedded lists page in more stories only once the in-place "show more" has revealed everything loaded. */}
      {!storiesQuery.isError && storiesQuery.hasNextPage && (!embedded || vm.more.hiddenCount === 0) ? (
        <Button
          variant="outline"
          className="mt-1 shrink-0"
          onClick={() => {
            void storiesQuery.fetchNextPage();
          }}
          disabled={storiesQuery.isFetchingNextPage}
        >
          {storiesQuery.isFetchingNextPage ? t('loading') : t('loadMore')}
        </Button>
      ) : null}
    </section>
  );
}

/** One compact story row: title, excerpt, count/time meta and publisher attribution. */
function StoryRow({ vm, story }: { vm: ReturnType<typeof buildStoryRenderers>; story: Story }) {
  const { onSelectStory, translation, shown } = vm;
  const excerpt = cleanExcerpt(shown(story)?.title ?? story.title, shown(story)?.excerpt ?? story.excerpt);
  const card = vm.embedded && vm.view === 'cards';
  return (
    <li data-testid={card ? 'news-card' : undefined} className={card ? 'min-w-0 rounded-md border border-border p-2' : undefined}>
      {card ? <FeedImage src={story.image_url} className="mb-2" /> : null}
      <Button
        type="button"
        variant="ghost"
        onClick={(event) => onSelectStory(story.id, event.currentTarget)}
        className={`h-auto w-full flex-col items-stretch justify-start whitespace-normal text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-none px-0 ${card ? 'py-1' : 'py-3'}`}
      >
        <span className="flex items-start justify-between gap-2">
          <span className="font-medium leading-snug">{shown(story)?.title ?? story.title}</span>
          <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        </span>
        {excerpt ? <span className="mt-1 line-clamp-3 text-sm text-muted-foreground">{excerpt}</span> : null}
        {!translation.results.has(story.id) && <TranslationNote issue={translation.issues.get(story.id)} />}
        <StoryRowMeta vm={vm} story={story} />
      </Button>
      <Attribution story={story} />
    </li>
  );
}

/** Count/time/relevance meta line and partial-data reasons under a story row. */
function StoryRowMeta({ vm, story }: { vm: StoryListVm; story: Story }) {
  const { t, display, incompleteLabel, relevanceVaries, relativeTime } = vm;
  return (
    <>
      <span
        className="mt-2 block text-xs text-muted-foreground"
        title={formatDateTime(story.observed_at, display.locale, display.timezone)}
      >
        {[
          // A lone source with a lone item carries no information, so only larger counts are shown.
          ...(story.source_count === 1 && story.evidence_count === 1
            ? []
            : [t('sources', { count: story.source_count }), t('evidence', { count: story.evidence_count })]),
          relativeTime(story.observed_at),
        ].join(' · ')}
        {story.relevance !== null && relevanceVaries ? ` · ${t('relevance')} ${Math.round(story.relevance * 100)}%` : null}
      </span>
      {story.incomplete_reasons.map((reason) => (
        <span key={reason} className="mt-1 block text-xs text-muted-foreground">
          {incompleteLabel(reason)}
        </span>
      ))}
    </>
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
      {t('publisher')}:{' '}
      {href ? (
        <a
          className="text-foreground underline underline-offset-2"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={item.license_label ?? undefined}
        >
          {item.publisher}
        </a>
      ) : (
        item.publisher
      )}
    </p>
  );
}

type StoryListVm = ReturnType<typeof buildStoryRenderers>;

/** Top row: slim meta row when embedded, otherwise page actions portaled to the host or an own header. */
function StoryListChrome({ vm }: { vm: StoryListVm }) {
  const { embedded, actionsHost, translation, incompleteNotice, showOriginal, setShowOriginal, refreshButton, pageActions } = vm;
  return embedded ? (
    // Slim meta row: partial-data notice on the left, translated badge + refresh on the right (no overlap with stories).
    <div className="flex h-6 shrink-0 items-center justify-between gap-2">
      <div className="min-w-0 truncate">{incompleteNotice}</div>
      <div className="flex items-center gap-1">
        {translation.results.size ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : null}
        <FeedViewToggle view={vm.view} setView={vm.setView} />
        {refreshButton}
      </div>
    </div>
  ) : actionsHost ? (
    createPortal(pageActions, actionsHost)
  ) : (
    <header className="flex items-center justify-end gap-1 border-b border-border pb-2">{pageActions}</header>
  );
}

/** Matching-scope trends as a chip strip when embedded or a list otherwise. */
function StoryTrends({ vm }: { vm: StoryListVm }) {
  const { t, trendItems, showTrends, embedded, onSelectStory } = vm;
  return showTrends && trendItems.length && embedded ? (
    // Embedded trends are a one-line chip strip (title + ratio) so stories stay the focus.
    <section aria-label={t('trends')} className="flex shrink-0 gap-2 overflow-x-auto pb-1">
      <h3 className="sr-only">{t('trends')}</h3>
      {trendItems.slice(0, 5).map((trend) => (
        <Button
          key={trend.story_id}
          type="button"
          variant="outline"
          size="sm"
          title={`${t('current24h')}: ${trend.current_count}`}
          onClick={(event) => onSelectStory(trend.story_id, event.currentTarget)}
          className="h-7 max-w-56 shrink-0 gap-1.5 rounded-full px-3 text-xs font-normal"
        >
          <span className="truncate">{trend.title}</span>
          <span className="tabular-nums text-muted-foreground">
            {trend.low_baseline ? t('lowBaseline') : `${trend.ratio?.toFixed(1)}×`}
          </span>
        </Button>
      ))}
    </section>
  ) : showTrends && trendItems.length ? (
    <section aria-label={t('trends')} className="space-y-2">
      <h3 className="text-xs font-medium text-muted-foreground">{t('trends')}</h3>
      {trendItems.slice(0, 5).map((trend) => (
        <Button
          key={trend.story_id}
          type="button"
          variant="ghost"
          onClick={(event) => onSelectStory(trend.story_id, event.currentTarget)}
          className="h-auto w-full flex-col items-stretch justify-start whitespace-normal rounded-md border border-border p-2 text-left text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="block text-sm font-medium">{trend.title}</span>
          <span className="text-xs text-muted-foreground">
            {t('current24h')}: {trend.current_count} · {trend.low_baseline ? t('lowBaseline') : `${trend.ratio?.toFixed(1)}×`}
          </span>
        </Button>
      ))}
    </section>
  ) : null;
}

/** Loading, error and empty states. */
function StoryListStatus({ vm }: { vm: StoryListVm }) {
  const { t, storiesQuery, trendQuery, stories, showTrends, embedded, noSources, incompleteNotice } = vm;
  return (
    <>
      {storiesQuery.isPending ? (
        <p role="status" className="text-sm text-muted-foreground">
          {t('loading')}
        </p>
      ) : null}
      {showTrends && trendQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {t('error')}
        </p>
      ) : null}
      {!embedded ? incompleteNotice : null}
      {storiesQuery.isError ? (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm text-destructive">
          <span>{t('error')}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void storiesQuery.refetch();
            }}
          >
            {t('retry')}
          </Button>
        </div>
      ) : null}
      {!storiesQuery.isPending && !storiesQuery.isError && stories.length === 0 ? (
        <div className="flex flex-col items-start gap-2 text-sm text-muted-foreground" data-testid="news-empty">
          {noSources ? (
            <>
              <p className="m-0">{t('emptyNoSources')}</p>
              <Button asChild variant="outline" size="sm">
                <Link href="/settings/sources">{t('addNewsSource')}</Link>
              </Button>
            </>
          ) : (
            <>
              <p className="m-0">{t('emptyAwaitingCollection')}</p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  void storiesQuery.refetch();
                }}
              >
                {t('refresh')}
              </Button>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}

/** Hero treatment of the lead story: image, large title, excerpt, source count and attribution. */
function StoryLead({ vm, story }: { vm: StoryListVm; story: Story }) {
  const { t, display, onSelectStory, shown, relativeTime } = vm;
  return (
    <div data-testid="news-lead" className="min-w-0">
      <FeedImage src={story.image_url} className="mb-2" />
      <Button
        type="button"
        variant="ghost"
        onClick={(event) => onSelectStory(story.id, event.currentTarget)}
        className="h-auto w-full flex-col items-stretch justify-start whitespace-normal rounded-none px-0 py-2 text-left font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="text-xl font-semibold leading-snug">{shown(story)?.title ?? story.title}</span>
        {cleanExcerpt(shown(story)?.title ?? story.title, shown(story)?.excerpt ?? story.excerpt) ? (
          <span className="mt-2 line-clamp-3 text-sm leading-relaxed text-muted-foreground">
            {cleanExcerpt(shown(story)?.title ?? story.title, shown(story)?.excerpt ?? story.excerpt)}
          </span>
        ) : null}
        <span className="mt-3 flex items-baseline gap-1.5 text-xs text-muted-foreground">
          {/* A single source is not a headline figure: show it as plain meta text instead. */}
          {story.source_count >= 2 ? (
            <>
              <span className="text-figure-sm text-foreground">{story.source_count}</span>
              <span>{t('sourcesLabel', { count: story.source_count })}</span>
            </>
          ) : (
            <span>{t('sources', { count: story.source_count })}</span>
          )}
          <span aria-hidden="true">·</span>
          <span className="tabular-nums" title={formatDateTime(story.observed_at, display.locale, display.timezone)}>
            {relativeTime(story.observed_at)}
          </span>
        </span>
      </Button>
      <Attribution story={story} />
    </div>
  );
}

/** Lead story beside the list when embedded, otherwise the plain story list. */
function StoryListBody({ vm }: { vm: StoryListVm }) {
  const { leadStory, more, view, embedded } = vm;
  const rows = more.visible;
  return leadStory ? (
    // Wide gadgets put the lead left and the list right; narrow ones stack them.
    <div className="grid shrink-0 gap-x-6 gap-y-2 @[40rem]/gadget:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <StoryLead vm={vm} story={leadStory} />
      <ol className="min-w-0 divide-y divide-border">
        {rows.map((story) => (
          <StoryRow key={story.id} vm={vm} story={story} />
        ))}
      </ol>
    </div>
  ) : (
    <ol
      className={
        embedded && view === 'cards' ? 'grid shrink-0 gap-3 @[40rem]/gadget:grid-cols-2' : 'shrink-0 divide-y divide-border'
      }
    >
      {rows.map((story) => (
        <StoryRow key={story.id} vm={vm} story={story} />
      ))}
    </ol>
  );
}

/** Flattens the loaded pages of an infinite query with the given per-page selector; empty before the first page. */
function flattenPages<P, T>(data: { pages: P[] } | undefined, pick: (page: P) => T[]): T[] {
  return data?.pages.flatMap(pick) ?? [];
}

/**
 * Embedded view state: the List/Cards layout (per gadget instance, persisted like the Feed gadget's) and the source tab.
 * The tab is the server-side source scope of the stories (trends keep the full selection); a tab whose source left the
 * selection falls back to All.
 */
function useStoryViewScope(instanceId: string | undefined, sourceIds: string[], embedded: boolean) {
  const [view, setView] = useLocalPref<FeedView>(`umwelt.dashboard.feedView.${instanceId ?? 'news'}`, 'list', FEED_VIEWS);
  const [pickedTab, setSourceTab] = useState('all');
  const sourceTab = pickedTab !== 'all' && sourceIds.includes(pickedTab) ? pickedTab : 'all';
  const storySourceIds = embedded && sourceTab !== 'all' ? [sourceTab] : sourceIds;
  return { view, setView, sourceTab, setSourceTab, storySourceIds };
}

/** Loads the paged stories and trends for the fixed source selection and derives the loaded items and incomplete reasons. */
function useStoryListData(props: StoryListProps) {
  const {
    sourceIds,
    topicId,
    query,
    limit = 25,
    showTrends = true,
    embedded = false,
    actionsHost,
    noSources = false,
    instanceId,
    sourceNames,
    onSelectStory,
  } = props;
  const t = useTranslations('news');
  const { view, setView, sourceTab, setSourceTab, storySourceIds } = useStoryViewScope(instanceId, sourceIds, embedded);
  const display = useDisplayPreferences();
  const incompleteLabel = useNewsIncompleteLabel();
  const storiesQuery = useInfiniteQuery({
    queryKey: ['news-stories', storySourceIds, topicId, query, limit],
    queryFn: ({ pageParam }) => fetchStories(storySourceIds, { cursor: pageParam, topicId, q: query, limit }),
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
  const stories = storiesQuery.isError ? [] : flattenPages(storiesQuery.data, (page) => page.items);
  const trendItems = trendQuery.isError ? [] : (trendQuery.data?.items ?? []);
  const incompleteReasons = new Set([
    ...(storiesQuery.isError ? [] : flattenPages(storiesQuery.data, (page) => page.incomplete_reasons)),
    ...(showTrends && !trendQuery.isError ? (trendQuery.data?.incomplete_reasons ?? []) : []),
  ]);

  return {
    t,
    display,
    incompleteLabel,
    storiesQuery,
    trendQuery,
    stories,
    trendItems,
    incompleteReasons,
    sourceIds,
    topicId,
    query,
    limit,
    showTrends,
    embedded,
    actionsHost,
    noSources,
    view,
    setView,
    sourceTab,
    setSourceTab,
    sourceNames,
    onSelectStory,
  };
}

/** Requests translations for loaded stories and derives the partial-data notice, relevance spread and lead story. */
function useStoryListPresentation(p: ReturnType<typeof useStoryListData>) {
  const { t, incompleteLabel, stories, incompleteReasons, embedded } = p;
  // Only the stories currently loaded are requested for translation.
  const targets = stories.filter((s) => s.translation_revision).map((s) => ({ id: s.id, revision: s.translation_revision as string }));
  const translation = useContentTranslation('news_story', targets);
  const [showOriginal, setShowOriginal] = useState(false);
  /** Translated title/excerpt for a story, or undefined when showing the original or no translation exists. */
  const shown = (story: Story) => (showOriginal ? undefined : (translation.results.get(story.id)?.translation ?? undefined));

  // Embedded gadgets show the partial-data notice in the slim top meta row.
  const incompleteNotice = incompleteReasons.size ? (
    <p role="status" className="text-xs text-muted-foreground" title={[...incompleteReasons].map(incompleteLabel).join(' ') || undefined}>
      {t('incompleteNotice')}
    </p>
  ) : null;

  // A relevance score identical on every visible story carries no ranking signal, so it is hidden.
  // Compare the rounded percentage actually displayed: 0.331 and 0.334 both read "33%".
  // Near-flat scores (spread under 10 percentage points) are noise too, so require a visible spread.
  const scores = stories.flatMap((s) => (s.relevance == null ? [] : [Math.round(s.relevance * 100)]));
  const relevanceVaries = scores.length > 1 && Math.max(...scores) - Math.min(...scores) >= 10;

  // Embedded lead: the story with the most sources (first on ties) gets the hero treatment.
  const leadStory =
    embedded && stories.length > 0
      ? stories.reduce((best, story) => (story.source_count > best.source_count ? story : best), stories[0])
      : null;
  // Cards mode drops the lead hero: every story is a card. Embedded lists collapse to the lead + 4 rows or 6 cards.
  const heroStory = p.view === 'cards' ? null : leadStory;
  const listStories = heroStory ? stories.filter((story) => story.id !== heroStory.id) : stories;
  const more = useShowMore(listStories, !embedded ? listStories.length : p.view === 'cards' ? 6 : 4, `${p.sourceTab}|${p.view}`);

  return {
    ...p,
    targets,
    translation,
    shown,
    incompleteNotice,
    scores,
    relevanceVaries,
    leadStory: heroStory,
    listStories,
    more,
    showOriginal,
    setShowOriginal,
  };
}

/** Builds the refresh button and the translated-badge plus refresh action group. */
function buildStoryListActions(p: ReturnType<typeof useStoryListPresentation>) {
  const { t, storiesQuery, trendQuery, showTrends, embedded, translation, showOriginal, setShowOriginal } = p;
  const refreshButton = (
    <Button
      variant="ghost"
      size="icon"
      className={embedded ? 'size-6' : undefined}
      onClick={() => {
        void storiesQuery.refetch();
        if (showTrends) void trendQuery.refetch();
      }}
      aria-label={t('retry')}
    >
      <RefreshCw className={`${embedded ? 'size-3.5' : 'size-4'} ${storiesQuery.isFetching ? 'animate-spin' : ''}`} />
    </Button>
  );

  const pageActions: ReactNode = (
    <>
      {translation.results.size ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : null}
      {refreshButton}
    </>
  );

  return { ...p, refreshButton, pageActions };
}

/** Provides relative-time formatting and the compact story row renderer. */
function buildStoryRenderers(p: ReturnType<typeof buildStoryListActions>) {
  const { display } = p;
  /** Relative time ("2 hr. ago") for list meta; the full datetime lives in the element title. */
  const relativeTime = (iso: string) =>
    formatRelativeTime(iso, normalizeFormattingLocale(display.locale)) || formatDateTime(iso, display.locale, display.timezone);

  return { ...p, relativeTime };
}
