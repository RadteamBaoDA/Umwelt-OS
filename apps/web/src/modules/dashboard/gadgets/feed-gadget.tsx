'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bookmark,
  ExternalLink,
  FileText,
  Newspaper,
  RotateCw,
  Rss,
  Send,
  ListFilter,
} from 'lucide-react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { useChatController } from '@/core/app-shell/chat-controller';
import { documentKeys, listGadgetDocumentProjections, setGadgetDocumentInteraction } from '@/modules/knowledge/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { NewsFeed } from './news-feed';
import { Sparkline } from './sparkline';
import { bucketByDay, formatRelativeTime } from './relative-time';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { dashboardKeys, listGadgetSources, type GadgetInstance } from '../api';

/** Feed item model supporting documents, news articles, and telegram messages. */
export interface FeedStreamItem {
  id: string;
  title: string;
  sourceType: 'news' | 'rss' | 'telegram' | 'document';
  author?: string | null;
  excerpt?: string;
  timestamp: string;
  url?: string | null;
  channelLabel?: string | null;
  messageId?: string | null;
  versionNumber?: number;
  sourceId?: string;
  documentVersionId?: string;
  publishedAt?: string | null;
  collectedAt?: string | null;
  editedAt?: string | null;
  media?: { kind: string; caption: string | null; count: number }[];
  read?: boolean;
  bookmarked?: boolean;
}

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

/** ISO start of a trailing window; evaluated at fetch time, not render time. */
const windowStart = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();

type SelectedTelegramIdentity = {
  sourceId: string;
  documentId: string;
  documentVersionId: string;
  versionNumber: number;
};

/** Keep provider-supplied links on explicit web schemes and Telegram hosts. */
function safeFeedUrl(value: string | null, telegram: boolean): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (telegram ? url.protocol !== 'https:' : !['https:', 'http:'].includes(url.protocol)) return null;
    if (telegram && !['t.me', 'www.t.me'].includes(url.hostname)) return null;
    return url.toString();
  } catch {
    return null;
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

/** Localized language name for an allowlisted code; falls back to the uppercase code. */
function languageLabel(locale: string, code: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: 'language' }).of(code) ?? code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

const WINDOW_HOURS: Record<string, number> = { '24h': 24, '7d': 168, '30d': 720 };
// Keep in sync with FEED_LANGUAGES in modules/knowledge/documents/public.py.
const FEED_LANGUAGES = ['en', 'vi', 'fr', 'de', 'es', 'pt', 'it', 'ru', 'ja', 'ko', 'zh', 'id', 'th'];

/** Renders the scrollable feed item stream with selection and Ask AI affordances. */
function FeedStream({
  instance,
  initialItems,
  onUnreadCountChange,
}: FeedGadgetProps) {
  const t = useTranslations('dashboard');
  const locale = useLocale();
  const display = useDisplayPreferences();
  const session = useWorkspaceSession();
  const { openDrawer } = useChatController();
  const queryClient = useQueryClient();

  const definition = instance.definition;
  const rendererType = definition.renderer;
  const isTelegram = rendererType === 'telegram_feed';
  const sourceIds = [...new Set(definition.source_ids)].slice(0, 32);
  const channelIds = rendererType === 'telegram_feed' ? (definition.scope.channel_ids ?? []).slice(0, 32) : [];

  // Determine source type from renderer
  const defaultSourceType: FeedStreamItem['sourceType'] =
    rendererType === 'telegram_feed'
      ? 'telegram'
      : rendererType === 'news_feed'
      ? 'news'
      : 'document';

  const [onlyUnread, setOnlyUnread] = useState<boolean>(false);
  // Row checkboxes appear only in selection mode so the resting list stays quiet.
  const [selectMode, setSelectMode] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [language, setLanguage] = useState('any');
  const [timeWindow, setTimeWindowRaw] = useState('any');
  // Lower bound captured when the window is chosen, so every page and refetch shares it.
  const [windowSince, setWindowSince] = useState<string | undefined>();
  /** Updates the feed time window filter. */
  const setTimeWindow = (value: string) => {
    const hours = WINDOW_HOURS[value];
    setWindowSince(hours ? windowStart(hours) : undefined);
    setTimeWindowRaw(value);
  };
  // Optimistic "Not relevant": version ids hidden locally until the server filter catches up (a new
  // version of the same document is not hidden); lastHidden powers Undo. Scope: dashboard feeds and highlights only.
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<string>>(new Set());
  const [lastHidden, setLastHidden] = useState<SelectedTelegramIdentity[]>([]);
  const [undoFailed, setUndoFailed] = useState(false);
  const [hideFailed, setHideFailed] = useState(false);
  const focusTarget = useRef<'undo' | 'root' | null>(null);
  const autoFetches = useRef({ scope: '', count: 0 });
  const undoRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const [selectedTelegram, setSelectedTelegram] = useState<Record<string, SelectedTelegramIdentity>>({});

  // Query documents as live feed items
  const docsQuery = useInfiniteQuery({
    queryKey: [...documentKeys.all, 'gadget', sourceIds, channelIds, language, windowSince],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      if (sourceIds.length === 0) return { items: [], next_cursor: null };
      return listGadgetDocumentProjections(sourceIds, channelIds, pageParam, {
        language: language === 'any' ? undefined : language,
        since: windowSince,
      });
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 30_000,
  });
  // Source names for the filter; only fetched when more than one source can be filtered.
  const sourcesQuery = useQuery({
    queryKey: [...dashboardKeys.sources, 'feed-filter'],
    queryFn: ({ signal }) => listGadgetSources(100, undefined, signal),
    enabled: sourceIds.length > 1,
  });
  const interactionMutation = useMutation({
    // Serialize hide/undo/read/save so an Undo can never overtake the Hide it reverses.
    scope: { id: `feed-interaction:${instance.id}` },
    mutationFn: (change: { id: string; versionNumber: number; item?: SelectedTelegramIdentity; read?: boolean; bookmarked?: boolean; dismissed?: boolean }) =>
      setGadgetDocumentInteraction(change.id, change.versionNumber, {
        read: change.read, bookmarked: change.bookmarked, dismissed: change.dismissed,
      }, session.csrfToken),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: documentKeys.all }),
    onError: (_error, change) => {
      if (change.dismissed === undefined || !change.item) return;
      const { item } = change;
      if (change.dismissed) {
        setHideFailed(true);
        setHiddenIds((current) => { const next = new Set(current); next.delete(item.documentVersionId); return next; });
      } else {
        // Undo failed: the server still has it hidden, so restore the hidden state and offer a retry.
        setHiddenIds((current) => new Set(current).add(item.documentVersionId));
        setLastHidden((current) => (current.some((c) => c.documentVersionId === item.documentVersionId) ? current : [...current, item]));
        setUndoFailed(true);
        focusTarget.current = 'undo';
      }
    },
  });
  /** Hides or restores the given Telegram identities from the feed. */
  const setHidden = (items: SelectedTelegramIdentity[], hidden: boolean) => {
    setHiddenIds((current) => {
      const next = new Set(current);
      items.forEach((item) => (hidden ? next.add(item.documentVersionId) : next.delete(item.documentVersionId)));
      return next;
    });
    setLastHidden(hidden ? items : []);
    setUndoFailed(false);
    setHideFailed(false);
    focusTarget.current = hidden ? 'undo' : 'root';
    items.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, item, dismissed: hidden }));
  };
  useEffect(() => {
    const target = focusTarget.current;
    if (!target || (target === 'undo' && !undoRef.current)) return;
    (target === 'undo' ? undoRef.current : rootRef.current)?.focus();
    focusTarget.current = null;
  }, [lastHidden, undoFailed]);

  // Transform raw documents or initial items into standard FeedStreamItem objects
  const feedItems: FeedStreamItem[] = useMemo(() => {
    if (initialItems && initialItems.length > 0) {
      return initialItems;
    }

    const docs = docsQuery.data?.pages.flatMap((page) => page.items) ?? [];
    return docs.flatMap((doc) => {
      const telegram = doc.provider_metadata?.provider === 'telegram' ? doc.provider_metadata.telegram : null;
      if (isTelegram && !telegram) return [];
      return {
        id: doc.document_id,
        title: doc.title,
        sourceType: defaultSourceType,
        author: typeof telegram?.channel_label === 'string' ? telegram.channel_label : null,
        channelLabel: typeof telegram?.channel_label === 'string' ? telegram.channel_label : null,
        messageId: typeof telegram?.message_id === 'string' ? telegram.message_id : null,
        excerpt: doc.excerpt,
        timestamp: doc.published_at || doc.observed_at,
        sourceId: doc.source_id,
        documentVersionId: doc.document_version_id,
        publishedAt: telegram?.published_at ?? doc.published_at,
        collectedAt: doc.observed_at,
        editedAt: telegram?.edited_at ?? null,
        media: telegram?.media ?? [],
        url: safeFeedUrl(doc.canonical_url, isTelegram),
        read: !!doc.read_at,
        bookmarked: !!doc.bookmarked_at,
        versionNumber: doc.version_number,
      };
    });
  }, [initialItems, docsQuery.data, defaultSourceType, isTelegram]);

  const unreadCount = feedItems.filter((i) => !i.read).length;
  // Item count moves to the frame footer next to freshness.
  useReportGadgetFreshness(
    feedItems.reduce<string | null>((max, i) => (i.collectedAt && (!max || i.collectedAt > max) ? i.collectedAt : max), null),
    feedItems.length > 0 ? t('gadgetItemCount', { count: feedItems.length }) : null,
  );
  // Client-side items-per-day series from the loaded items; the spark shows only with 2+ days.
  const volume = useMemo(() => bucketByDay(feedItems.map((i) => i.timestamp)), [feedItems]);
  const selectedTelegramItems = Object.values(selectedTelegram);
  const currentTelegramItems = new Map(feedItems.map((item) => [item.id, item]));
  const staleTelegramSelections = selectedTelegramItems.filter((item) =>
    currentTelegramItems.get(item.documentId)?.documentVersionId !== item.documentVersionId,
  );
  const needle = searchText.trim().toLowerCase();
  const displayedItems = feedItems.filter((i) =>
    !(i.documentVersionId && hiddenIds.has(i.documentVersionId))
    && (!onlyUnread || !i.read)
    && (sourceFilter === 'all' || i.sourceId === sourceFilter)
    && (!needle || `${i.title} ${i.excerpt ?? ''}`.toLowerCase().includes(needle)),
  );
  const filtering = onlyUnread || sourceFilter !== 'all' || needle !== '';
  const activeFilterCount = Number(needle !== '') + Number(sourceFilter !== 'all') + Number(language !== 'any') + Number(timeWindow !== 'any');
  const serverFiltered = language !== 'any' || timeWindow !== 'any';
  // Search/language/time controls are noise on a settled empty feed with nothing active to clear.
  const showFilters = docsQuery.isLoading || feedItems.length > 0 || filtering || serverFiltered;
  React.useEffect(() => onUnreadCountChange?.(unreadCount), [onUnreadCountChange, unreadCount]);

  // Server pages can be short or fully hidden: while nothing is visible and a cursor exists, pull
  // the next page (at most 3 times) instead of claiming the feed is empty.
  const serverScope = `${sourceIds.join()}|${channelIds.join()}|${language}|${timeWindow}`;
  useEffect(() => {
    const budget = autoFetches.current;
    if (budget.scope !== serverScope || displayedItems.length > 0) { budget.scope = serverScope; budget.count = 0; }
    if (filtering || displayedItems.length > 0 || initialItems?.length || !docsQuery.hasNextPage || docsQuery.isFetching || docsQuery.isError || budget.count >= 3) return;
    budget.count += 1;
    void docsQuery.fetchNextPage();
  }, [displayedItems.length, initialItems, docsQuery, serverScope, filtering]);

  /** Per-item actions (read, save, source, details, Ask AI) revealed on hover/focus so rows stay quiet at rest. */
  const renderActions = (item: FeedStreamItem) => {
    const isRead = !!item.read;
    return (
      <div className="absolute right-0 top-0 flex flex-wrap items-center gap-1 bg-card pl-2 text-xs pointer-coarse:static pointer-coarse:w-full pointer-coarse:bg-transparent pointer-coarse:pl-0 pointer-coarse:opacity-100 text-muted-foreground opacity-0 transition-opacity group-hover/item:opacity-100 group-focus-within/item:opacity-100">
        {isTelegram && item.documentVersionId && item.sourceId && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => openDrawer({ context: { kind: 'selection', items: [{
              sourceId: item.sourceId!, documentId: item.id,
              documentVersionId: item.documentVersionId!,
            }] } })}
          >{t('askAboutMessage')}</Button>
        )}
        {item.versionNumber !== undefined && <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          disabled={interactionMutation.isPending}
          onClick={() => interactionMutation.mutate({ id: item.id, versionNumber: item.versionNumber!, read: !item.read })}
        >{isRead ? t('markUnread') : t('markRead')}</Button>}
        {item.versionNumber !== undefined && <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-6"
          disabled={interactionMutation.isPending}
          onClick={() => interactionMutation.mutate({ id: item.id, versionNumber: item.versionNumber!, bookmarked: !item.bookmarked })}
          aria-pressed={!!item.bookmarked}
          aria-label={item.bookmarked ? t('removeBookmark') : t('bookmark')}
        ><Bookmark className="h-3.5 w-3.5" fill={item.bookmarked ? 'currentColor' : 'none'} aria-hidden="true" /></Button>}
        {safeHttpUrl(item.url) && (
          <a
            href={safeHttpUrl(item.url)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 font-medium hover:text-foreground"
          >
            <span>{t('sourceLink')}</span>
            <ExternalLink className="w-2.5 h-2.5" />
          </a>
        )}
        <Link href={`/knowledge/documents/${item.id}`} className="font-medium hover:text-foreground">{t('detailLink')}</Link>
      </div>
    );
  };

  /** Selection checkbox; rendered only while selection mode is on, capped at 32 identities. */
  const renderCheckbox = (item: FeedStreamItem) => selectMode && item.documentVersionId && item.sourceId ? (
    <Checkbox
      aria-label={t('selectRecord', { title: item.title })}
      checked={Boolean(selectedTelegram[item.id])}
      onCheckedChange={(next) => setSelectedTelegram((current) => {
        if (next !== true) {
          const remaining = { ...current };
          delete remaining[item.id];
          return remaining;
        }
        if (Object.keys(current).length >= 32) return current;
        return { ...current, [item.id]: {
          sourceId: item.sourceId!,
          documentId: item.id,
          documentVersionId: item.documentVersionId!,
          versionNumber: item.versionNumber!,
        } };
      })}
    />
  ) : null;

  /** Single muted meta line: source/channel, then (for Telegram) message id. Time is rendered separately. */
  const metaParts = (item: FeedStreamItem) => [
    isTelegram ? item.channelLabel : item.author,
    isTelegram && item.messageId ? t('telegramMessageId', { id: item.messageId }) : null,
  ].filter(Boolean).join(' · ');

  /** Full timestamps for a tooltip; Telegram folds published/collected/edited lines into one title. */
  const timeTitle = (item: FeedStreamItem) => isTelegram
    ? [
      item.publishedAt && t('telegramPublishedAt', { time: formatDateTime(item.publishedAt, display.locale, display.timezone) }),
      item.collectedAt && t('telegramCollectedAt', { time: formatDateTime(item.collectedAt, display.locale, display.timezone) }),
      item.editedAt && t('telegramEditedAt', { time: formatDateTime(item.editedAt, display.locale, display.timezone) }),
    ].filter(Boolean).join('\n')
    : formatDateTime(item.timestamp, display.locale, display.timezone);

  /** Unread marker: a 6px ink dot with screen-reader text, replacing the old per-item pill. */
  const unreadDot = (item: FeedStreamItem) => item.read ? null : (
    <span className="inline-block size-1.5 shrink-0 self-center rounded-full bg-foreground"><span className="sr-only">{t('unreadBadge', { count: 1 })}</span></span>
  );

  /** Telegram media placeholder list shared by lead and rows. */
  const mediaList = (item: FeedStreamItem) => isTelegram && item.media && item.media.length > 0 ? (
    <ul aria-label={t('telegramMedia')} className="space-y-0.5 text-xs text-muted-foreground">
      {item.media.map((media, index) => (
        <li key={`${media.kind}:${index}`}>
          {t('telegramMediaPlaceholder', { kind: t(`telegramMediaKind_${media.kind}`), count: media.count })}
          {media.caption ? ` · ${media.caption}` : ''}
        </li>
      ))}
    </ul>
  ) : null;

  const [lead, ...rest] = displayedItems;

  return (
    <div ref={rootRef} tabIndex={-1} className="flex h-full flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card px-3 pb-3 pt-2 text-card-foreground focus:outline-none">
      {/* One quiet toolbar: unread toggle, volume spark, Filter popover, selection mode, refresh. */}
      <div className="flex items-center justify-between gap-2 text-xs">
        <Button
          type="button"
          variant={onlyUnread ? 'default' : 'ghost'}
          size="sm"
          className="h-8 px-2 text-xs"
          aria-pressed={onlyUnread}
          onClick={() => setOnlyUnread((prev) => !prev)}
        >
          {t('unreadOnly')} ({unreadCount})
        </Button>

        <div className="flex min-w-0 items-center gap-1">
          {volume.length >= 2 && (
            <span className="w-20 shrink-0" title={t('feedVolume')}><Sparkline data={volume} height={24} width={80} line /></span>
          )}
          {showFilters && (
            <Popover>
              <PopoverTrigger asChild>
                <Button type="button" variant="ghost" size="sm" className="h-8 gap-1 px-2 text-xs">
                  <ListFilter className="h-3.5 w-3.5" aria-hidden="true" />
                  {activeFilterCount > 0 ? t('feedFilterActive', { count: activeFilterCount }) : t('feedFilter')}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-72 space-y-2 p-3">
                <Input
                  type="search"
                  value={searchText}
                  onChange={(event) => setSearchText(event.target.value)}
                  aria-label={t('feedSearch')}
                  placeholder={t('feedSearch')}
                  className="h-9 text-xs"
                />
                {sourceIds.length > 1 && (
                  <Select value={sourceFilter} onValueChange={setSourceFilter}>
                    <SelectTrigger aria-label={t('feedSourceLabel')} className="h-9 w-full text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all" className="text-xs">{t('feedSourceAll')}</SelectItem>
                      {sourceIds.map((id) => (
                        <SelectItem key={id} value={id} className="text-xs">
                          {sourcesQuery.data?.items.find((source) => source.id === id)?.name ?? t('feedSourceUnknown')}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
                <Select value={language} onValueChange={setLanguage}>
                  <SelectTrigger aria-label={t('feedLanguageLabel')} className="h-9 w-full text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="any" className="text-xs">{t('feedLanguageAny')}</SelectItem>
                    {FEED_LANGUAGES.map((code) => <SelectItem key={code} value={code} className="text-xs">{languageLabel(locale, code)}</SelectItem>)}
                  </SelectContent>
                </Select>
                <Select value={timeWindow} onValueChange={setTimeWindow}>
                  <SelectTrigger aria-label={t('feedWindowLabel')} className="h-9 w-full text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="any" className="text-xs">{t('feedWindowAny')}</SelectItem>
                    <SelectItem value="24h" className="text-xs">{t('feedWindow24h')}</SelectItem>
                    <SelectItem value="7d" className="text-xs">{t('feedWindow7d')}</SelectItem>
                    <SelectItem value="30d" className="text-xs">{t('feedWindow30d')}</SelectItem>
                  </SelectContent>
                </Select>
                {filtering && <p className="text-xs text-muted-foreground">{t('feedFiltersNote')}</p>}
              </PopoverContent>
            </Popover>
          )}
          {feedItems.some((item) => item.documentVersionId && item.sourceId) && (
            <Button
              type="button"
              variant={selectMode ? 'default' : 'ghost'}
              size="sm"
              className="h-8 px-2 text-xs"
              aria-pressed={selectMode}
              onClick={() => { setSelectMode((prev) => !prev); setSelectedTelegram({}); }}
            >{selectMode ? t('feedSelectDone') : t('feedSelectMode')}</Button>
          )}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={() => docsQuery.refetch()}
            disabled={docsQuery.isFetching}
            aria-label={t('feedRefresh')}
          >
            <RotateCw className={`w-3.5 h-3.5 ${docsQuery.isFetching ? 'animate-spin' : ''}`} aria-hidden="true" />
          </Button>
        </div>
      </div>

      {selectedTelegramItems.length > 0 && (
        <div role="group" aria-label={t('feedBulkBar')} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background p-2 text-xs">
          <span className="font-semibold" role="status">{t('feedSelectedCount', { count: selectedTelegramItems.length })}</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => openDrawer({ context: {
              kind: 'selection',
              items: selectedTelegramItems.map(({ sourceId, documentId, documentVersionId }) => ({
                sourceId, documentId, documentVersionId,
              })),
            } })}
          >{t('askAboutSelected', { count: selectedTelegramItems.length })}</Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={interactionMutation.isPending}
            onClick={() => selectedTelegramItems.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, read: true }))}
          >{t('feedMarkRead')}</Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={interactionMutation.isPending}
            onClick={() => selectedTelegramItems.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, bookmarked: true }))}
          >{t('feedSave')}</Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => { setHidden(selectedTelegramItems, true); setSelectedTelegram({}); }}
          >{t('feedNotRelevant')}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setSelectedTelegram({})}>{t('feedClear')}</Button>
        </div>
      )}

      {/* The live region stays mounted so screen readers announce the text when it changes. */}
      <div className={lastHidden.length > 0 ? 'flex items-center gap-2 rounded-md border border-border bg-background p-2 text-xs' : 'sr-only'}>
        <span role="status" aria-live="polite">{lastHidden.length > 0 ? t('feedHiddenCount', { count: lastHidden.length }) : ''}</span>
        {lastHidden.length > 0 && (
          <Button
            ref={undoRef}
            type="button"
            size="sm"
            variant="ghost"
            aria-disabled={interactionMutation.isPending || undefined}
            onClick={() => { if (!interactionMutation.isPending) setHidden(lastHidden, false); }}
          >{undoFailed ? t('feedUndoRetry') : t('feedUndo')}</Button>
        )}
      </div>
      {hideFailed && <p role="alert" className="text-xs text-destructive">{t('feedHideError')}</p>}
      {undoFailed && <p role="alert" className="text-xs text-destructive">{t('feedUndoError')}</p>}

      {staleTelegramSelections.length > 0 && (
        <p role="status" className="text-xs text-muted-foreground">{t('selectionChanged', { count: staleTelegramSelections.length })}</p>
      )}

      {/* Loading Skeleton */}
      {docsQuery.isLoading && feedItems.length === 0 && (
        <GadgetSkeleton variant="list" label={t('gadgetDataLoading')} className="flex-1 px-0" />
      )}

      {docsQuery.isError && !docsQuery.isFetchNextPageError && (
        <GadgetNotice kind="error" message={t('feedLoadError')} className="px-0" />
      )}

      {/* Empty State */}
      {!docsQuery.isLoading && !docsQuery.isError && (!docsQuery.hasNextPage || filtering) && displayedItems.length === 0 && (
        <GadgetNotice kind="empty" message={`${t('feedEmptyTitle')}. ${needle || sourceFilter !== 'all'
          ? t('feedNoMatch')
          : serverFiltered
            ? t('feedNoServerMatch')
            : onlyUnread
              ? t('feedCaughtUp')
              : t('feedEmptyDetail')}`} action={needle || sourceFilter !== 'all' || serverFiltered || onlyUnread ? undefined : { href: '/settings/sources', label: t('gadgetConfigureAction') }} className="px-0" />
      )}

      {/* Lead + list: one DOM per item; the container query turns the first item into a lead on wide gadgets. */}
      {lead && (
        <div className="grid min-h-0 gap-x-6 gap-y-1 @[40rem]/gadget:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <article
            data-testid="feed-lead"
            className="group/item relative flex min-w-0 items-baseline gap-2 py-1.5 @[40rem]/gadget:block @[40rem]/gadget:py-0 @[40rem]/gadget:pr-2"
          >
            {renderCheckbox(lead)}
            <div className="flex min-w-0 flex-1 items-baseline gap-2 @[40rem]/gadget:block">
              <h3 className={`flex min-w-0 flex-1 items-baseline gap-1.5 text-sm leading-snug @[40rem]/gadget:text-xl ${lead.read ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'}`}>
                {unreadDot(lead)}
                <Link href={`/knowledge/documents/${lead.id}`} className="min-w-0 truncate hover:underline @[40rem]/gadget:line-clamp-2 @[40rem]/gadget:whitespace-normal">{lead.title}</Link>
              </h3>
              {lead.excerpt && <p className="mt-2 hidden line-clamp-3 text-sm leading-relaxed text-muted-foreground @[40rem]/gadget:block">{lead.excerpt}</p>}
              <p className="shrink-0 text-xs text-muted-foreground @[40rem]/gadget:mt-2">
                {metaParts(lead) ? `${metaParts(lead)} · ` : ''}
                <time className="tabular-nums" dateTime={lead.timestamp} title={timeTitle(lead)}>{formatRelativeTime(lead.timestamp, display.locale)}</time>
              </p>
              <div className="hidden @[40rem]/gadget:block">{mediaList(lead)}</div>
            </div>
            {renderActions(lead)}
          </article>

          <ul className="min-w-0 divide-y divide-border/50">
            {rest.map((item) => (
              <li key={item.id} className="group/item relative flex flex-wrap items-baseline gap-2 py-1.5">
                {renderCheckbox(item)}
                {unreadDot(item)}
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/knowledge/documents/${item.id}`}
                    className={`block truncate text-sm leading-snug hover:underline ${item.read ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'}`}
                  >{item.title}</Link>
                  {isTelegram && metaParts(item) && <p className="truncate text-xs text-muted-foreground">{metaParts(item)}</p>}
                  {mediaList(item)}
                </div>
                <time className="shrink-0 text-xs tabular-nums text-muted-foreground" dateTime={item.timestamp} title={timeTitle(item)}>{formatRelativeTime(item.timestamp, display.locale)}</time>
                {renderActions(item)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!initialItems?.length && docsQuery.isFetchNextPageError && (
        <p role="alert" className="text-xs text-destructive">{t('feedLoadMoreError')}</p>
      )}
      {!initialItems?.length && docsQuery.hasNextPage && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="self-center"
          disabled={docsQuery.isFetchingNextPage}
          onClick={() => docsQuery.fetchNextPage()}
        >{docsQuery.isFetchingNextPage ? t('feedLoadingMore') : t('feedLoadMore')}</Button>
      )}
      {!initialItems?.length && !docsQuery.hasNextPage && !docsQuery.isError && feedItems.length > 0 && (
        <p className="text-center text-xs text-muted-foreground">{t('feedEndOfList')}</p>
      )}
    </div>
  );
}
