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
} from 'lucide-react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { useChatController } from '@/core/app-shell/chat-controller';
import { documentKeys, listGadgetDocumentProjections, setGadgetDocumentInteraction } from '@/modules/knowledge/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { NewsFeed } from './news-feed';
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
        <TabsTrigger value="items" className="min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none">
          {t('feedTabItems')}
        </TabsTrigger>
        <TabsTrigger value="stories" className="min-h-11 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none">
          {t('feedTabStories')}
        </TabsTrigger>
      </TabsList>
      <TabsContent value="items" className="mt-0 min-h-0 flex-1 overflow-hidden"><FeedStream {...props} /></TabsContent>
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
  const [searchText, setSearchText] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [language, setLanguage] = useState('any');
  const [timeWindow, setTimeWindowRaw] = useState('any');
  // Lower bound captured when the window is chosen, so every page and refetch shares it.
  const [windowSince, setWindowSince] = useState<string | undefined>();
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
  const setHidden = (items: SelectedTelegramIdentity[], hidden: boolean) => {
    setHiddenIds((current) => {
      const next = new Set(current);
      items.forEach((item) => (hidden ? next.add(item.documentVersionId) : next.delete(item.documentVersionId)));
      return next;
    });
    setLastHidden(hidden ? items : []);
    setUndoFailed(false);
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
  const serverFiltered = language !== 'any' || timeWindow !== 'any';
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

  return (
    <div ref={rootRef} tabIndex={-1} className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto overflow-x-hidden focus:outline-none">
      {/* Top action bar */}
      <div className="flex items-center justify-between border-b border-border pb-2 text-xs">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setOnlyUnread((prev) => !prev)}
            className={`px-2 py-0.5 rounded font-medium transition-colors ${
              onlyUnread
                ? 'bg-primary text-primary-foreground font-semibold'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {onlyUnread ? t('unreadOnly') : t('allFeedItems')} ({feedItems.length})
          </button>
          {unreadCount > 0 && (
            <span className="px-1.5 py-0.2 rounded-full text-[10px] font-bold bg-primary/20 text-primary">
            {t('unreadCount', { count: unreadCount })}
            </span>
          )}
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => docsQuery.refetch()}
            disabled={docsQuery.isFetching}
            className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors"
            title="Refresh feed"
            aria-label="Refresh feed stream"
          >
            <RotateCw className={`w-3.5 h-3.5 ${docsQuery.isFetching ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Search and source filters apply to the loaded items only */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type="search"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          aria-label={t('feedSearch')}
          placeholder={t('feedSearch')}
          className="h-11 min-w-0 flex-1 basis-40 text-xs"
        />
        {sourceIds.length > 1 && (
          <Select value={sourceFilter} onValueChange={setSourceFilter}>
            <SelectTrigger aria-label={t('feedSourceLabel')} className="h-11 w-auto min-w-36 text-xs">
              <SelectValue />
            </SelectTrigger>
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
          <SelectTrigger aria-label={t('feedLanguageLabel')} className="h-11 w-auto min-w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any" className="text-xs">{t('feedLanguageAny')}</SelectItem>
            {FEED_LANGUAGES.map((code) => <SelectItem key={code} value={code} className="text-xs">{languageLabel(locale, code)}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={timeWindow} onValueChange={setTimeWindow}>
          <SelectTrigger aria-label={t('feedWindowLabel')} className="h-11 w-auto min-w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any" className="text-xs">{t('feedWindowAny')}</SelectItem>
            <SelectItem value="24h" className="text-xs">{t('feedWindow24h')}</SelectItem>
            <SelectItem value="7d" className="text-xs">{t('feedWindow7d')}</SelectItem>
            <SelectItem value="30d" className="text-xs">{t('feedWindow30d')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {filtering && <p className="text-[11px] text-muted-foreground">{t('feedFiltersNote')}</p>}

      {selectedTelegramItems.length > 0 && (
        <div role="group" aria-label={t('feedBulkBar')} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background p-2 text-xs">
          <span className="font-semibold" role="status">{t('feedSelectedCount', { count: selectedTelegramItems.length })}</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="min-h-11"
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
            className="min-h-11"
            disabled={interactionMutation.isPending}
            onClick={() => selectedTelegramItems.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, read: true }))}
          >{t('feedMarkRead')}</Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="min-h-11"
            disabled={interactionMutation.isPending}
            onClick={() => selectedTelegramItems.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, bookmarked: true }))}
          >{t('feedSave')}</Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="min-h-11"
            onClick={() => { setHidden(selectedTelegramItems, true); setSelectedTelegram({}); }}
          >{t('feedNotRelevant')}</Button>
          <Button type="button" size="sm" variant="ghost" className="min-h-11" onClick={() => setSelectedTelegram({})}>{t('feedClear')}</Button>
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
            className="min-h-11"
            aria-disabled={interactionMutation.isPending || undefined}
            onClick={() => { if (!interactionMutation.isPending) setHidden(lastHidden, false); }}
          >{undoFailed ? t('feedUndoRetry') : t('feedUndo')}</Button>
        )}
      </div>
      {undoFailed && <p role="alert" className="text-xs text-destructive">{t('feedUndoError')}</p>}

      {staleTelegramSelections.length > 0 && (
        <p role="status" className="text-xs text-muted-foreground">{t('selectionChanged', { count: staleTelegramSelections.length })}</p>
      )}

      {/* Loading Skeleton */}
      {docsQuery.isLoading && feedItems.length === 0 && (
        <div className="flex-1 space-y-2 p-1 animate-pulse">
          <div className="h-16 bg-muted/20 rounded-lg" />
          <div className="h-16 bg-muted/20 rounded-lg" />
          <div className="h-16 bg-muted/20 rounded-lg" />
        </div>
      )}

      {docsQuery.isError && !docsQuery.isFetchNextPageError && (
        <p role="alert" className="text-xs text-destructive">{t('feedLoadError')}</p>
      )}

      {/* Empty State */}
      {!docsQuery.isLoading && !docsQuery.isError && (!docsQuery.hasNextPage || filtering) && displayedItems.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <Newspaper className="w-8 h-8 mb-2 opacity-50" />
            <p className="text-xs font-semibold text-foreground mb-1">{t('feedEmptyTitle')}</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            {needle || sourceFilter !== 'all'
              ? t('feedNoMatch')
              : serverFiltered
              ? t('feedNoServerMatch')
              : onlyUnread
              ? t('feedCaughtUp')
              : t('feedEmptyDetail')}
          </p>
        </div>
      )}

      {/* Stream Items List */}
      {displayedItems.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-2 min-h-0 pr-0.5">
          {displayedItems.map((item) => {
            const isRead = !!item.read;
            const formattedTime = formatDateTime(
              item.timestamp,
              display.locale,
              display.timezone,
            );

            return (
              <article
                key={item.id}
                className={`p-2.5 rounded-lg border transition-all space-y-1.5 ${
                  isRead
                    ? 'border-border/60 bg-card/60 opacity-80'
                    : 'border-border bg-card hover:bg-muted/15 shadow-xs'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="p-1 rounded bg-muted/30 text-primary shrink-0">
                      {getFeedSourceIcon(item.sourceType)}
                    </span>
                    <h5
                      className={`text-xs leading-snug line-clamp-1 ${
                        isRead ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'
                      }`}
                    >
                      {item.title}
                    </h5>
                  </div>

                  {!isRead && (
                    <span className="px-1.5 py-0.2 rounded text-[10px] font-bold bg-primary/20 text-primary uppercase tracking-wider shrink-0">
                      {t('unreadBadge')}
                    </span>
                  )}
                </div>

                {isTelegram && (item.channelLabel || item.messageId) && (
                  <p className="text-[10px] text-muted-foreground">
                    {[item.channelLabel, item.messageId ? t('telegramMessageId', { id: item.messageId }) : null].filter(Boolean).join(' · ')}
                  </p>
                )}

                {item.documentVersionId && item.sourceId && (
                  <label className="inline-flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    <input
                      type="checkbox"
                      aria-label={t('selectRecord', { title: item.title })}
                      checked={Boolean(selectedTelegram[item.id])}
                      onChange={(event) => setSelectedTelegram((current) => {
                        if (!event.target.checked) {
                          const next = { ...current };
                          delete next[item.id];
                          return next;
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
                    {t('selectRow')}
                  </label>
                )}

                {item.excerpt && (
                  <p className="text-[11px] text-muted-foreground line-clamp-2 leading-relaxed">
                    {item.excerpt}
                  </p>
                )}

                {isTelegram && item.media && item.media.length > 0 && (
                  <ul aria-label={t('telegramMedia')} className="space-y-1 text-[10px] text-muted-foreground">
                    {item.media.map((media, index) => (
                      <li key={`${media.kind}:${index}`}>
                        {t('telegramMediaPlaceholder', { kind: t(`telegramMediaKind_${media.kind}`), count: media.count })}
                        {media.caption ? ` · ${media.caption}` : ''}
                      </li>
                    ))}
                  </ul>
                )}

                {/* Footer metadata & actions */}
                <div className="flex items-center justify-between text-[10px] text-muted-foreground pt-1 border-t border-border/40">
                  {isTelegram ? (
                    <div className="space-y-0.5">
                      {item.publishedAt && <p>{t('telegramPublishedAt', { time: formatDateTime(item.publishedAt, display.locale, display.timezone) })}</p>}
                      {item.collectedAt && <p>{t('telegramCollectedAt', { time: formatDateTime(item.collectedAt, display.locale, display.timezone) })}</p>}
                      {item.editedAt && <p>{t('telegramEditedAt', { time: formatDateTime(item.editedAt, display.locale, display.timezone) })}</p>}
                    </div>
                  ) : <span className="font-mono">{formattedTime}</span>}

                  <div className="flex items-center gap-2">
                    {isTelegram && item.documentVersionId && item.sourceId && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-6 px-1.5 text-[10px]"
                        onClick={() => openDrawer({ context: { kind: 'selection', items: [{
                          sourceId: item.sourceId!, documentId: item.id,
                          documentVersionId: item.documentVersionId!,
                        }] } })}
                      >{t('askAboutMessage')}</Button>
                    )}
                    {item.versionNumber !== undefined && <button
                      type="button"
                      disabled={interactionMutation.isPending}
                      onClick={() => interactionMutation.mutate({ id: item.id, versionNumber: item.versionNumber!, read: !item.read })}
                      className="hover:text-foreground font-medium transition-colors"
                    >
                      {isRead ? t('markUnread') : t('markRead')}
                    </button>}
                    {item.versionNumber !== undefined && <button
                      type="button"
                      disabled={interactionMutation.isPending}
                      onClick={() => interactionMutation.mutate({ id: item.id, versionNumber: item.versionNumber!, bookmarked: !item.bookmarked })}
                      aria-pressed={!!item.bookmarked}
                      className="hover:text-foreground"
                      title={item.bookmarked ? t('removeBookmark') : t('bookmark')}
                    ><Bookmark className="h-3.5 w-3.5" fill={item.bookmarked ? 'currentColor' : 'none'} /></button>}

                    {safeHttpUrl(item.url) && (
                      <a
                        href={safeHttpUrl(item.url)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-0.5 hover:text-primary font-medium"
                      >
                        <span>{t('sourceLink')}</span>
                        <ExternalLink className="w-2.5 h-2.5" />
                      </a>
                    )}

                    <Link
                      href={`/knowledge/documents/${item.id}`}
                      className="hover:text-primary font-medium"
                    >
                      {t('detailLink')}
                    </Link>
                  </div>
                </div>
              </article>
            );
          })}
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
          className="min-h-11 self-center"
          disabled={docsQuery.isFetchingNextPage}
          onClick={() => docsQuery.fetchNextPage()}
        >{docsQuery.isFetchingNextPage ? t('feedLoadingMore') : t('feedLoadMore')}</Button>
      )}
      {!initialItems?.length && !docsQuery.hasNextPage && !docsQuery.isError && feedItems.length > 0 && (
        <p className="text-center text-[11px] text-muted-foreground">{t('feedEndOfList')}</p>
      )}
    </div>
  );
}
