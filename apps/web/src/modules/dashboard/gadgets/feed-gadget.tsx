'use client';

import { useQuery } from '@tanstack/react-query';
import {
  Bookmark,
  CheckCheck,
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
import { documentKeys, listDocuments, type Document } from '@/modules/knowledge/api';
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
  unread: boolean;
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

  const definition = instance.definition;
  const rendererType = definition.renderer;

  // Determine source type from renderer
  const defaultSourceType: FeedStreamItem['sourceType'] =
    rendererType === 'telegram_feed'
      ? 'telegram'
      : rendererType === 'news_feed'
      ? 'news'
      : 'document';

  // Local unread items tracking
  const [readStateMap, setReadStateMap] = useState<Record<string, boolean>>({});
  const [onlyUnread, setOnlyUnread] = useState<boolean>(false);

  // Query documents as live feed items
  const docsQuery = useQuery({
    queryKey: documentKeys.all,
    queryFn: async () => {
      const page = await listDocuments();
      return page.items;
    },
    staleTime: 30_000,
  });

  // Transform raw documents or initial items into standard FeedStreamItem objects
  const feedItems: FeedStreamItem[] = useMemo(() => {
    if (initialItems && initialItems.length > 0) {
      return initialItems;
    }

    const docs = docsQuery.data ?? [];
    return docs.map((doc) => {
      const isLocallyRead = readStateMap[doc.id] ?? false;
      return {
        id: doc.id,
        title: doc.title,
        sourceType: defaultSourceType,
        author: doc.author,
        excerpt:
          typeof doc.metadata?.summary === 'string'
            ? doc.metadata.summary
            : `Document version ${doc.current_version} • Hash ${doc.content_hash.slice(0, 8)}`,
        timestamp: doc.published_at || doc.observed_at || doc.created_at,
        url: doc.canonical_url,
        unread: !isLocallyRead,
      };
    });
  }, [initialItems, docsQuery.data, readStateMap, defaultSourceType]);

  // Mark all as read handler
  const handleMarkAllRead = () => {
    const nextMap: Record<string, boolean> = {};
    for (const item of feedItems) {
      nextMap[item.id] = true;
    }
    setReadStateMap(nextMap);
    onUnreadCountChange?.(0);
  };

  // Toggle single item read status
  const handleToggleItemRead = (itemId: string) => {
    setReadStateMap((prev) => ({
      ...prev,
      [itemId]: !prev[itemId],
    }));
  };

  const unreadCount = feedItems.filter((i) => !readStateMap[i.id]).length;
  const displayedItems = onlyUnread ? feedItems.filter((i) => !readStateMap[i.id]) : feedItems;

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
            {onlyUnread ? 'Unread only' : 'All items'} ({feedItems.length})
          </button>
          {unreadCount > 0 && (
            <span className="px-1.5 py-0.2 rounded-full text-[10px] font-bold bg-primary/20 text-primary">
              {unreadCount} unread
            </span>
          )}
        </div>

        <div className="flex items-center gap-1">
          {unreadCount > 0 && (
            <button
              type="button"
              onClick={handleMarkAllRead}
              className="text-[11px] font-medium text-muted-foreground hover:text-foreground flex items-center gap-1 mr-1"
              title="Mark all items as read"
            >
              <CheckCheck className="w-3.5 h-3.5" />
              <span>Mark read</span>
            </button>
          )}

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
          <p className="text-xs font-semibold text-foreground mb-1">Feed stream empty</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            {onlyUnread
              ? 'You have caught up with all unread items.'
              : 'Incoming items from your configured sources will appear here.'}
          </p>
        </div>
      )}

      {/* Stream Items List */}
      {displayedItems.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-2 min-h-0 pr-0.5">
          {displayedItems.map((item) => {
            const isRead = !!readStateMap[item.id];
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
                      Unread
                    </span>
                  )}
                </div>

                {item.excerpt && (
                  <p className="text-[11px] text-muted-foreground line-clamp-2 leading-relaxed">
                    {item.excerpt}
                  </p>
                )}

                {/* Footer metadata & actions */}
                <div className="flex items-center justify-between text-[10px] text-muted-foreground pt-1 border-t border-border/40">
                  <span className="font-mono">{formattedTime}</span>

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => handleToggleItemRead(item.id)}
                      className="hover:text-foreground font-medium transition-colors"
                    >
                      {isRead ? 'Mark unread' : 'Mark read'}
                    </button>

                    {item.url && (
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-0.5 hover:text-primary font-medium"
                      >
                        <span>Source</span>
                        <ExternalLink className="w-2.5 h-2.5" />
                      </a>
                    )}

                    <Link
                      href={`/knowledge/documents/${item.id}`}
                      className="hover:text-primary font-medium"
                    >
                      Detail
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
