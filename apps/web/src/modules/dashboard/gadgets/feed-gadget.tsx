'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bookmark,
  ExternalLink,
  Eye,
  FileText,
  Newspaper,
  RotateCw,
  Rss,
  Send,
} from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useChatController } from '@/core/app-shell/chat-controller';
import { documentKeys, listGadgetDocumentProjections, setGadgetDocumentInteraction } from '@/modules/knowledge/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import type { GadgetInstance } from '../api';

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
 *
 * @param props Gadget instance configuration and feed handlers.
 * @returns Accessible feed stream gadget component.
 */
export function FeedGadget({
  instance,
  initialItems,
  onUnreadCountChange,
}: FeedGadgetProps) {
  const t = useTranslations('dashboard');
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
  const [selectedTelegram, setSelectedTelegram] = useState<Record<string, SelectedTelegramIdentity>>({});

  // Query documents as live feed items
  const docsQuery = useQuery({
    queryKey: [...documentKeys.all, 'gadget', sourceIds, channelIds],
    queryFn: async () => {
      if (sourceIds.length === 0) return { items: [], next_cursor: null };
      return listGadgetDocumentProjections(sourceIds, channelIds);
    },
    staleTime: 30_000,
  });
  const interactionMutation = useMutation({
    mutationFn: (change: { id: string; versionNumber: number; read?: boolean; bookmarked?: boolean }) =>
      setGadgetDocumentInteraction(change.id, change.versionNumber, {
        read: change.read, bookmarked: change.bookmarked,
      }, session.csrfToken),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: documentKeys.all }),
  });

  // Transform raw documents or initial items into standard FeedStreamItem objects
  const feedItems: FeedStreamItem[] = useMemo(() => {
    if (initialItems && initialItems.length > 0) {
      return initialItems;
    }

    const docs = docsQuery.data?.items ?? [];
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
  const displayedItems = onlyUnread ? feedItems.filter((i) => !i.read) : feedItems;
  React.useEffect(() => onUnreadCountChange?.(unreadCount), [onUnreadCountChange, unreadCount]);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-hidden">
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
          {isTelegram && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={selectedTelegramItems.length === 0}
              onClick={() => openDrawer({ context: {
                kind: 'selection',
                items: selectedTelegramItems.map(({ sourceId, documentId, documentVersionId }) => ({
                  sourceId, documentId, documentVersionId,
                })),
              } })}
            >{t('askAboutSelected', { count: selectedTelegramItems.length })}</Button>
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

      {isTelegram && staleTelegramSelections.length > 0 && (
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

      {/* Empty State */}
      {!docsQuery.isLoading && displayedItems.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <Newspaper className="w-8 h-8 mb-2 opacity-50" />
            <p className="text-xs font-semibold text-foreground mb-1">{t('feedEmptyTitle')}</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            {onlyUnread
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

                {isTelegram && item.documentVersionId && item.sourceId && (
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

                    {item.url && (
                      <a
                        href={item.url}
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
    </div>
  );
}
