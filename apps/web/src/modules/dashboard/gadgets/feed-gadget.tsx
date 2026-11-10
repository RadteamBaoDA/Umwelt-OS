'use client';

import { FileText, Newspaper, Rss, Send } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useMemo, useRef, useState } from 'react';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useChatController } from '@/core/app-shell/chat-controller';
import { NewsFeed } from './news-feed';
import { Sparkline } from './sparkline';
import { bucketByDay } from './relative-time';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import type { GadgetInstance } from '../api';
import {
  defaultSourceTypeFor, documentsToItems, filterFeedItems, newestCollectedAt, staleSelections,
  type FeedStreamItem, type SelectedTelegramIdentity,
} from './feed-model';
import {
  FEED_VIEWS, FeedBulkBar, FeedFilterPopover, FeedItems, FeedToolbar, FeedUndoBar, type FeedRowContext, type FeedView,
} from './feed-parts';
import { useLocalPref } from '@/core/use-local-pref';
import { useAutoFetchWhenEmpty, useFeedFilters, useFeedModeration, useFeedQueries } from './use-feed-state';

export type { FeedStreamItem } from './feed-model';

/** Props for the FeedGadget component. */
export interface FeedGadgetProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional pre-loaded items for preview or SSR. */
  initialItems?: FeedStreamItem[];
  /** Callback notifying parent frame of unread count change. */
  onUnreadCountChange?: (count: number) => void;
}

/**
 * Returns a suitable icon for the feed source category.
 *
 * @param type Feed source classification.
 * @returns React icon element.
 */
export function getFeedSourceIcon(type: FeedStreamItem['sourceType']): React.ReactElement {
  switch (type) {
    case 'telegram':
      return <Send className="w-3.5 h-3.5" />;
    case 'rss':
      return <Rss className="w-3.5 h-3.5" />;
    case 'news':
      return <Newspaper className="w-3.5 h-3.5" />;
    default:
      return <FileText className="w-3.5 h-3.5" />;
  }
}

/**
 * Standard Feed Stream gadget template.
 * Displays real-time or polled items from news feeds, RSS channels, Telegram messages,
 * and knowledge documents, with visible unread badges and reading stability protection.
 * Its fixed loading surface and root scroll fallback fit the frame's bounded reading floor.
 *
 * @param props Gadget instance configuration and feed handlers.
 * @returns Accessible feed stream gadget component.
 */
export function FeedGadget(props: FeedGadgetProps) {
  const t = useTranslations('dashboard');
  const [tab, setTab] = useState<'items' | 'stories'>('items');
  // Story clusters come from the news module; Telegram records have no story clusters.
  if (props.instance.definition.renderer === 'telegram_feed') return <FeedStream {...props} />;
  return (
    <Tabs value={tab} onValueChange={(value) => setTab(value as 'items' | 'stories')} className="flex h-full min-h-0 flex-col">
      <TabsList aria-label={t('feedTabs')} className="h-auto justify-start gap-1 rounded-none bg-transparent p-0 px-3 pt-2">
        <TabsTrigger value="items" className="min-h-9 pointer-coarse:min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none">
          {t('feedTabItems')}
        </TabsTrigger>
        <TabsTrigger value="stories" className="min-h-9 pointer-coarse:min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none">
          {t('feedTabStories')}
        </TabsTrigger>
      </TabsList>
      <TabsContent value="items" forceMount className="mt-0 min-h-0 flex-1 overflow-hidden data-[state=inactive]:hidden"><FeedStream {...props} /></TabsContent>
      <TabsContent value="stories" className="mt-0 min-h-0 flex-1 overflow-hidden"><NewsFeed instance={props.instance} /></TabsContent>
    </Tabs>
  );
}

/** Settled-empty notice: explains why nothing is shown and offers "configure sources" only when nothing is filtering. */
function FeedEmptyNotice({ needle, sourceFilter, serverFiltered, onlyUnread }: {
  needle: string; sourceFilter: string; serverFiltered: boolean; onlyUnread: boolean;
}) {
  const t = useTranslations('dashboard');
  const searching = Boolean(needle) || sourceFilter !== 'all';
  const emptyDetail = searching
    ? t('feedNoMatch')
    : serverFiltered ? t('feedNoServerMatch') : onlyUnread ? t('feedCaughtUp') : t('feedEmptyDetail');
  return (
    <GadgetNotice kind="empty" message={`${t('feedEmptyTitle')}. ${emptyDetail}`} action={searching || serverFiltered || onlyUnread ? undefined : { href: '/settings/sources', label: t('gadgetConfigureAction') }} className="px-0" />
  );
}

/** Empty / error / loading status blocks of the feed (shown instead of the item list). */
function FeedStatus(props: {
  docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'];
  feedItemCount: number;
  displayedCount: number;
  filtering: boolean;
  needle: string;
  sourceFilter: string;
  serverFiltered: boolean;
  onlyUnread: boolean;
}) {
  const t = useTranslations('dashboard');
  const { docsQuery, needle, sourceFilter, serverFiltered, onlyUnread, filtering } = props;
  return (
    <>
      {/* Loading Skeleton */}
      {docsQuery.isLoading && props.feedItemCount === 0 && (
        <GadgetSkeleton variant="list" label={t('gadgetDataLoading')} className="flex-1 px-0" />
      )}

      {docsQuery.isError && !docsQuery.isFetchNextPageError && (
        <GadgetNotice kind="error" message={t('feedLoadError')} className="px-0" />
      )}

      {/* Empty State */}
      {!docsQuery.isLoading && !docsQuery.isError && (!docsQuery.hasNextPage || filtering) && props.displayedCount === 0 && (
        <FeedEmptyNotice needle={needle} sourceFilter={sourceFilter} serverFiltered={serverFiltered} onlyUnread={onlyUnread} />
      )}
    </>
  );
}

/** Pagination footer: load-more error, load-more button or the end-of-list marker. */
function FeedFooter({ docsQuery, hasInitialItems, feedItemCount }: {
  docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'];
  hasInitialItems: boolean;
  feedItemCount: number;
}) {
  const t = useTranslations('dashboard');
  if (hasInitialItems) return null;
  return (
    <>
      {docsQuery.isFetchNextPageError && (
        <p role="alert" className="text-xs text-destructive">{t('feedLoadMoreError')}</p>
      )}
      {docsQuery.hasNextPage && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="self-center"
          disabled={docsQuery.isFetchingNextPage}
          onClick={() => docsQuery.fetchNextPage()}
        >{docsQuery.isFetchingNextPage ? t('feedLoadingMore') : t('feedLoadMore')}</Button>
      )}
      {!docsQuery.hasNextPage && !docsQuery.isError && feedItemCount > 0 && (
        <p className="text-center text-xs text-muted-foreground">{t('feedEndOfList')}</p>
      )}
    </>
  );
}

/**
 * Derives the list of feed items from preloaded items or the loaded document pages.
 * Preloaded items (preview / SSR) win when present.
 */
function useFeedItems(initialItems: FeedStreamItem[] | undefined, docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'], renderer: string): FeedStreamItem[] {
  const isTelegram = renderer === 'telegram_feed';
  const defaultSourceType = defaultSourceTypeFor(renderer);
  return useMemo(() => {
    if (initialItems && initialItems.length > 0) return initialItems;
    const docs = docsQuery.data?.pages.flatMap((page) => page.items) ?? [];
    return documentsToItems(docs, isTelegram, defaultSourceType);
  }, [initialItems, docsQuery.data, defaultSourceType, isTelegram]);
}

/** Summarises the active filters: trimmed search needle, whether client filtering applies and how many filters are set. */
function describeFilters(filters: ReturnType<typeof useFeedFilters>) {
  const { onlyUnread, searchText, sourceFilter, language, timeWindow } = filters;
  const needle = searchText.trim().toLowerCase();
  return {
    needle,
    filtering: onlyUnread || sourceFilter !== 'all' || needle !== '',
    activeFilterCount: Number(needle !== '') + Number(sourceFilter !== 'all') + Number(language !== 'any') + Number(timeWindow !== 'any'),
    serverFiltered: language !== 'any' || timeWindow !== 'any',
  };
}

/** Renders the scrollable feed item stream with selection and Ask AI affordances. */
function FeedStream({
  instance,
  initialItems,
  onUnreadCountChange,
}: FeedGadgetProps) {
  const t = useTranslations('dashboard');
  const { openDrawer } = useChatController();
  const definition = instance.definition;
  const isTelegram = definition.renderer === 'telegram_feed';
  const sourceIds = [...new Set(definition.source_ids)].slice(0, 32);
  const channelIds = isTelegram ? (definition.scope.channel_ids ?? []).slice(0, 32) : [];
  const filters = useFeedFilters();
  // Layout choice is per gadget instance and survives reloads (unlike the transient filters).
  const [view, setView] = useLocalPref<FeedView>(`umwelt.dashboard.feedView.${instance.id}`, 'list', FEED_VIEWS);
  const { onlyUnread, selectMode, searchText, sourceFilter, language, timeWindow } = filters;
  const [selectedTelegram, setSelectedTelegram] = useState<Record<string, SelectedTelegramIdentity>>({});
  const { docsQuery, sourcesQuery } = useFeedQueries({ sourceIds, channelIds, language, windowSince: filters.windowSince });
  const undoRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const moderation = useFeedModeration(instance.id, { undoRef, rootRef });
  const { interactionMutation } = moderation;

  const feedItems = useFeedItems(initialItems, docsQuery, definition.renderer);
  const unreadCount = feedItems.filter((i) => !i.read).length;
  // Item count moves to the frame footer next to freshness.
  useReportGadgetFreshness(
    newestCollectedAt(feedItems),
    feedItems.length > 0 ? t('gadgetItemCount', { count: feedItems.length }) : null,
  );
  // Client-side items-per-day series from the loaded items; the spark shows only with 2+ days.
  const volume = useMemo(() => bucketByDay(feedItems.map((i) => i.timestamp)), [feedItems]);
  const selectedTelegramItems = Object.values(selectedTelegram);
  const { needle, filtering, activeFilterCount, serverFiltered } = describeFilters(filters);
  const displayedItems = filterFeedItems(feedItems, { hiddenIds: moderation.hiddenIds, onlyUnread, sourceFilter, needle });
  // Search/language/time controls are noise on a settled empty feed with nothing active to clear.
  const showFilters = docsQuery.isLoading || feedItems.length > 0 || filtering || serverFiltered;
  React.useEffect(() => onUnreadCountChange?.(unreadCount), [onUnreadCountChange, unreadCount]);
  const serverScope = `${sourceIds.join()}|${channelIds.join()}|${language}|${timeWindow}`;
  useAutoFetchWhenEmpty({ docsQuery, displayedCount: displayedItems.length, filtering, hasInitialItems: Boolean(initialItems?.length), serverScope });

  const askAbout: FeedRowContext['onAskAbout'] = (items) => openDrawer({ context: { kind: 'selection', items } });
  const rowContext: FeedRowContext = {
    view, isTelegram, selectMode, selected: selectedTelegram, setSelected: setSelectedTelegram,
    interactionPending: interactionMutation.isPending, onInteract: (change) => interactionMutation.mutate(change), onAskAbout: askAbout,
  };
  const staleCount = staleSelections(selectedTelegramItems, feedItems).length;

  return (
    <div ref={rootRef} tabIndex={-1} className="flex h-full flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card px-[var(--gadget-pad)] pb-[var(--gadget-pad)] pt-2 text-card-foreground focus:outline-none">
      <FeedToolbar
        onlyUnread={onlyUnread} toggleUnread={() => filters.setOnlyUnread((prev) => !prev)} unreadCount={unreadCount}
        volume={volume.length >= 2 && (
          <span className="w-20 shrink-0" title={t('feedVolume')}><Sparkline data={volume} height={24} width={80} line /></span>
        )}
        filters={showFilters && (
          <FeedFilterPopover
            searchText={searchText} setSearchText={filters.setSearchText} sourceIds={sourceIds}
            sourceFilter={sourceFilter} setSourceFilter={filters.setSourceFilter} sourceNames={sourcesQuery.data?.items}
            language={language} setLanguage={filters.setLanguage} timeWindow={timeWindow} setTimeWindow={filters.setTimeWindow}
            activeFilterCount={activeFilterCount} filtering={filtering}
          />
        )}
        canSelect={feedItems.some((item) => item.documentVersionId && item.sourceId)} selectMode={selectMode}
        toggleSelectMode={() => { filters.setSelectMode((prev) => !prev); setSelectedTelegram({}); }}
        refreshing={docsQuery.isFetching} onRefresh={() => docsQuery.refetch()}
        view={view} setView={setView}
      />
      <FeedBulkBar
        items={selectedTelegramItems} interactionPending={interactionMutation.isPending} onAskAbout={askAbout}
        onInteract={(change) => interactionMutation.mutate(change)}
        onHide={(items) => { moderation.setHidden(items, true); setSelectedTelegram({}); }} onClear={() => setSelectedTelegram({})}
      />
      <FeedUndoBar
        lastHidden={moderation.lastHidden} undoFailed={moderation.undoFailed} hideFailed={moderation.hideFailed}
        interactionPending={interactionMutation.isPending} undoRef={undoRef}
        onUndo={() => moderation.setHidden(moderation.lastHidden, false)}
      />
      {staleCount > 0 && (
        <p role="status" className="text-xs text-muted-foreground">{t('selectionChanged', { count: staleCount })}</p>
      )}
      <FeedStatus
        docsQuery={docsQuery} feedItemCount={feedItems.length} displayedCount={displayedItems.length} filtering={filtering}
        needle={needle} sourceFilter={sourceFilter} serverFiltered={serverFiltered} onlyUnread={onlyUnread}
      />
      <FeedItems items={displayedItems} ctx={rowContext} />
      <FeedFooter docsQuery={docsQuery} hasInitialItems={Boolean(initialItems?.length)} feedItemCount={feedItems.length} />
    </div>
  );
}
