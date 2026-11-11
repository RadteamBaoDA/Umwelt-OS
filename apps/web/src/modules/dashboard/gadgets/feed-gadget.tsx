'use client';

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
  defaultSourceTypeFor,
  documentsToItems,
  filterFeedItems,
  newestCollectedAt,
  staleSelections,
  type FeedStreamItem,
  type SelectedTelegramIdentity,
} from './feed-model';
import {
  FEED_VIEWS,
  FeedBulkBar,
  FeedFilterPopover,
  FeedItems,
  FeedSourceTabs,
  FeedToolbar,
  FeedUndoBar,
  type FeedRowContext,
  type FeedView,
} from './feed-parts';
import { ShowMoreButton, useShowMore } from './show-more';
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
        <TabsTrigger
          value="items"
          className="min-h-9 pointer-coarse:min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none"
        >
          {t('feedTabItems')}
        </TabsTrigger>
        <TabsTrigger
          value="stories"
          className="min-h-9 pointer-coarse:min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none"
        >
          {t('feedTabStories')}
        </TabsTrigger>
      </TabsList>
      <TabsContent value="items" forceMount className="mt-0 min-h-0 flex-1 overflow-hidden data-[state=inactive]:hidden">
        <FeedStream {...props} />
      </TabsContent>
      <TabsContent value="stories" className="mt-0 min-h-0 flex-1 overflow-hidden">
        <NewsFeed instance={props.instance} />
      </TabsContent>
    </Tabs>
  );
}

/** Settled-empty notice: explains why nothing is shown and offers "configure sources" only when nothing is filtering. */
function FeedEmptyNotice({
  needle,
  sourceFilter,
  serverFiltered,
  onlyUnread,
}: {
  needle: string;
  sourceFilter: string;
  serverFiltered: boolean;
  onlyUnread: boolean;
}) {
  const t = useTranslations('dashboard');
  const searching = Boolean(needle) || sourceFilter !== 'all';
  const emptyDetail = searching
    ? t('feedNoMatch')
    : serverFiltered
      ? t('feedNoServerMatch')
      : onlyUnread
        ? t('feedCaughtUp')
        : t('feedEmptyDetail');
  return (
    <GadgetNotice
      kind="empty"
      message={`${t('feedEmptyTitle')}. ${emptyDetail}`}
      action={searching || serverFiltered || onlyUnread ? undefined : { href: '/settings/sources', label: t('gadgetConfigureAction') }}
      className="px-0"
    />
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

      {docsQuery.isError && !docsQuery.isFetchNextPageError && <GadgetNotice kind="error" message={t('feedLoadError')} className="px-0" />}

      {/* Empty State */}
      {!docsQuery.isLoading && !docsQuery.isError && (!docsQuery.hasNextPage || filtering) && props.displayedCount === 0 && (
        <FeedEmptyNotice needle={needle} sourceFilter={sourceFilter} serverFiltered={serverFiltered} onlyUnread={onlyUnread} />
      )}
    </>
  );
}

/** Pagination footer: load-more error, load-more button or the end-of-list marker. */
function FeedFooter({
  docsQuery,
  hasInitialItems,
  feedItemCount,
  listExpanded,
}: {
  docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'];
  hasInitialItems: boolean;
  feedItemCount: number;
  /** False while the local "Show more" list still hides loaded items; server paging waits until it is fully shown. */
  listExpanded: boolean;
}) {
  const t = useTranslations('dashboard');
  if (hasInitialItems) return null;
  return (
    <>
      {docsQuery.isFetchNextPageError && (
        <p role="alert" className="text-xs text-destructive">
          {t('feedLoadMoreError')}
        </p>
      )}
      {docsQuery.hasNextPage && listExpanded && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="self-center"
          disabled={docsQuery.isFetchingNextPage}
          onClick={() => docsQuery.fetchNextPage()}
        >
          {docsQuery.isFetchingNextPage ? t('feedLoadingMore') : t('feedLoadMore')}
        </Button>
      )}
      {!docsQuery.hasNextPage && !docsQuery.isError && feedItemCount > 0 && listExpanded && (
        <p className="text-center text-xs text-muted-foreground">{t('feedEndOfList')}</p>
      )}
    </>
  );
}

/**
 * Derives the list of feed items from preloaded items or the loaded document pages.
 * Preloaded items (preview / SSR) win when present.
 */
function useFeedItems(
  initialItems: FeedStreamItem[] | undefined,
  docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'],
  renderer: string,
): FeedStreamItem[] {
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

/** Collapsed list size: lead + 4 rows, or 6 cards; any filter/view change (`filterKey`) collapses it again. */
function useFeedShowMore(items: FeedStreamItem[], view: FeedView, filterKey: string) {
  return useShowMore(items, view === 'cards' ? 6 : 5, `${filterKey}|${view}`);
}

/**
 * Feed toolbar wired to the transient filters: unread toggle, items-per-day spark (2+ days only), the filter
 * popover (hidden when `showFilters` is false), select mode, refresh and the per-instance view choice.
 */
function FeedStreamToolbar(props: {
  filters: ReturnType<typeof useFeedFilters>;
  unreadCount: number;
  volume: ReturnType<typeof bucketByDay>;
  showFilters: boolean;
  sourceIds: string[];
  sourceNames: { id: string; name: string }[] | undefined;
  activeFilterCount: number;
  filtering: boolean;
  canSelect: boolean;
  onToggleSelect: () => void;
  refreshing: boolean;
  onRefresh: () => void;
  view: FeedView;
  setView: (view: FeedView) => void;
}) {
  const t = useTranslations('dashboard');
  const { filters, volume } = props;
  return (
    <FeedToolbar
      onlyUnread={filters.onlyUnread}
      toggleUnread={() => filters.setOnlyUnread((prev) => !prev)}
      unreadCount={props.unreadCount}
      volume={
        volume.length >= 2 && (
          <span className="w-20 shrink-0" title={t('feedVolume')}>
            <Sparkline data={volume} height={24} width={80} line />
          </span>
        )
      }
      filters={
        props.showFilters && (
          <FeedFilterPopover
            searchText={filters.searchText}
            setSearchText={filters.setSearchText}
            sourceIds={props.sourceIds}
            sourceFilter={filters.sourceFilter}
            setSourceFilter={filters.setSourceFilter}
            sourceNames={props.sourceNames}
            language={filters.language}
            setLanguage={filters.setLanguage}
            timeWindow={filters.timeWindow}
            setTimeWindow={filters.setTimeWindow}
            activeFilterCount={props.activeFilterCount}
            filtering={props.filtering}
          />
        )
      }
      canSelect={props.canSelect}
      selectMode={filters.selectMode}
      toggleSelectMode={props.onToggleSelect}
      refreshing={props.refreshing}
      onRefresh={props.onRefresh}
      view={props.view}
      setView={props.setView}
    />
  );
}

/**
 * Bulk-selection bar, hide/undo bar and the notice for selections whose items left the feed. Hiding a
 * selection clears it; undo restores the last hidden batch.
 */
function FeedModerationBars({
  moderation,
  selected,
  clearSelected,
  staleCount,
  undoRef,
  onAskAbout,
}: {
  moderation: ReturnType<typeof useFeedModeration>;
  selected: SelectedTelegramIdentity[];
  clearSelected: () => void;
  staleCount: number;
  undoRef: React.RefObject<HTMLButtonElement | null>;
  onAskAbout: FeedRowContext['onAskAbout'];
}) {
  const t = useTranslations('dashboard');
  const { interactionMutation } = moderation;
  return (
    <>
      <FeedBulkBar
        items={selected}
        interactionPending={interactionMutation.isPending}
        onAskAbout={onAskAbout}
        onInteract={(change) => interactionMutation.mutate(change)}
        onHide={(items) => {
          moderation.setHidden(items, true);
          clearSelected();
        }}
        onClear={clearSelected}
      />
      <FeedUndoBar
        lastHidden={moderation.lastHidden}
        undoFailed={moderation.undoFailed}
        hideFailed={moderation.hideFailed}
        interactionPending={interactionMutation.isPending}
        undoRef={undoRef}
        onUndo={() => moderation.setHidden(moderation.lastHidden, false)}
      />
      {staleCount > 0 && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('selectionChanged', { count: staleCount })}
        </p>
      )}
    </>
  );
}

/**
 * Derived feed signals: reports freshness and the item count to the gadget frame footer, notifies the parent of
 * the unread count, and builds the client-side items-per-day series (the spark shows only with 2+ days).
 */
function useFeedSignals(feedItems: FeedStreamItem[], onUnreadCountChange: FeedGadgetProps['onUnreadCountChange']) {
  const t = useTranslations('dashboard');
  const unreadCount = feedItems.filter((i) => !i.read).length;
  useReportGadgetFreshness(newestCollectedAt(feedItems), feedItems.length > 0 ? t('gadgetItemCount', { count: feedItems.length }) : null);
  const volume = useMemo(() => bucketByDay(feedItems.map((i) => i.timestamp)), [feedItems]);
  React.useEffect(() => onUnreadCountChange?.(unreadCount), [onUnreadCountChange, unreadCount]);
  return { unreadCount, volume };
}

/** Renders the scrollable feed item stream with selection and Ask AI affordances. */
function FeedStream({ instance, initialItems, onUnreadCountChange }: FeedGadgetProps) {
  const { openDrawer } = useChatController();
  const definition = instance.definition;
  const isTelegram = definition.renderer === 'telegram_feed';
  const sourceIds = [...new Set(definition.source_ids)].slice(0, 32);
  const channelIds = isTelegram ? (definition.scope.channel_ids ?? []).slice(0, 32) : [];
  const filters = useFeedFilters();
  // Layout choice is per gadget instance and survives reloads (unlike the transient filters).
  const [view, setView] = useLocalPref<FeedView>(`umwelt.dashboard.feedView.${instance.id}`, 'list', FEED_VIEWS);
  const { onlyUnread, selectMode, sourceFilter, language, timeWindow } = filters;
  const [selectedTelegram, setSelectedTelegram] = useState<Record<string, SelectedTelegramIdentity>>({});
  const { docsQuery, sourcesQuery } = useFeedQueries({ sourceIds, channelIds, language, windowSince: filters.windowSince });
  const undoRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const moderation = useFeedModeration(instance.id, { undoRef, rootRef });

  const feedItems = useFeedItems(initialItems, docsQuery, definition.renderer);
  const { unreadCount, volume } = useFeedSignals(feedItems, onUnreadCountChange);
  const selectedTelegramItems = Object.values(selectedTelegram);
  const { needle, filtering, activeFilterCount, serverFiltered } = describeFilters(filters);
  const displayedItems = filterFeedItems(feedItems, { hiddenIds: moderation.hiddenIds, onlyUnread, sourceFilter, needle });
  const more = useFeedShowMore(displayedItems, view, `${sourceFilter}|${needle}|${onlyUnread}|${language}|${timeWindow}`);
  // Search/language/time controls are noise on a settled empty feed with nothing active to clear.
  const showFilters = docsQuery.isLoading || feedItems.length > 0 || filtering || serverFiltered;
  const serverScope = `${sourceIds.join()}|${channelIds.join()}|${language}|${timeWindow}`;
  useAutoFetchWhenEmpty({
    docsQuery,
    displayedCount: displayedItems.length,
    filtering,
    hasInitialItems: Boolean(initialItems?.length),
    serverScope,
  });

  const askAbout: FeedRowContext['onAskAbout'] = (items) => openDrawer({ context: { kind: 'selection', items } });
  const rowContext: FeedRowContext = {
    view,
    isTelegram,
    selectMode,
    selected: selectedTelegram,
    setSelected: setSelectedTelegram,
    interactionPending: moderation.interactionMutation.isPending,
    onInteract: (change) => moderation.interactionMutation.mutate(change),
    onAskAbout: askAbout,
  };

  return (
    <div
      ref={rootRef}
      tabIndex={-1}
      className="flex h-full flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card px-[var(--gadget-pad)] pb-[var(--gadget-pad)] pt-2 text-card-foreground focus:outline-none"
    >
      <FeedStreamToolbar
        filters={filters}
        unreadCount={unreadCount}
        volume={volume}
        showFilters={showFilters}
        sourceIds={sourceIds}
        sourceNames={sourcesQuery.data?.items}
        activeFilterCount={activeFilterCount}
        filtering={filtering}
        canSelect={feedItems.some((item) => item.documentVersionId && item.sourceId)}
        onToggleSelect={() => {
          filters.setSelectMode((prev) => !prev);
          setSelectedTelegram({});
        }}
        refreshing={docsQuery.isFetching}
        onRefresh={() => docsQuery.refetch()}
        view={view}
        setView={setView}
      />
      <FeedSourceTabs
        sourceIds={sourceIds}
        sourceNames={sourcesQuery.data?.items}
        value={sourceFilter}
        onChange={filters.setSourceFilter}
      />
      <FeedModerationBars
        moderation={moderation}
        selected={selectedTelegramItems}
        clearSelected={() => setSelectedTelegram({})}
        staleCount={staleSelections(selectedTelegramItems, feedItems).length}
        undoRef={undoRef}
        onAskAbout={askAbout}
      />
      <FeedStatus
        docsQuery={docsQuery}
        feedItemCount={feedItems.length}
        displayedCount={displayedItems.length}
        filtering={filtering}
        needle={needle}
        sourceFilter={sourceFilter}
        serverFiltered={serverFiltered}
        onlyUnread={onlyUnread}
      />
      <FeedItems items={more.visible} ctx={rowContext} />
      <ShowMoreButton state={more} />
      <FeedFooter
        docsQuery={docsQuery}
        hasInitialItems={Boolean(initialItems?.length)}
        feedItemCount={feedItems.length}
        listExpanded={more.hiddenCount === 0}
      />
    </div>
  );
}
