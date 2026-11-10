import type { listGadgetDocumentProjections } from '@/modules/knowledge';

/** Pure feed model: item shape, URL safety, document projection and client-side filtering. */

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
  /** Same-origin signed proxy path for the item image; null/absent when none or remote media is off. */
  imageUrl?: string | null;
}

/** Identity of one Telegram record version, used for selection, hiding and bulk actions. */
export type SelectedTelegramIdentity = {
  sourceId: string;
  documentId: string;
  documentVersionId: string;
  versionNumber: number;
};

/** Server-side time windows offered by the filter popover, in hours. */
export const WINDOW_HOURS: Record<string, number> = { '24h': 24, '7d': 168, '30d': 720 };
// Keep in sync with FEED_LANGUAGES in modules/knowledge/documents/public.py.
export const FEED_LANGUAGES = ['en', 'vi', 'fr', 'de', 'es', 'pt', 'it', 'ru', 'ja', 'ko', 'zh', 'id', 'th'];

/** ISO start of a trailing window; evaluated at fetch time, not render time. */
export const windowStart = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();

/** Localized language name for an allowlisted code; falls back to the uppercase code. */
export function languageLabel(locale: string, code: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: 'language' }).of(code) ?? code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

/** Keep provider-supplied links on explicit web schemes and Telegram hosts. */
export function safeFeedUrl(value: string | null, telegram: boolean): string | null {
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

/** Determines the item source type from the renderer id. */
export function defaultSourceTypeFor(renderer: string): FeedStreamItem['sourceType'] {
  if (renderer === 'telegram_feed') return 'telegram';
  return renderer === 'news_feed' ? 'news' : 'document';
}

/** One dashboard document projection row as returned by the knowledge API. */
export type FeedDocument = Awaited<ReturnType<typeof listGadgetDocumentProjections>>['items'][number];

/** Telegram metadata block of a document, or null for other providers. */
function telegramOf(doc: FeedDocument) {
  return doc.provider_metadata?.provider === 'telegram' ? doc.provider_metadata.telegram : null;
}

/** Returns the value when it is a string, otherwise null. */
const asString = (value: unknown): string | null => (typeof value === 'string' ? value : null);

/** Maps one document to a feed item, or nothing when a Telegram feed meets a non-Telegram document. */
function documentToItem(doc: FeedDocument, isTelegram: boolean, sourceType: FeedStreamItem['sourceType']): FeedStreamItem[] {
  const telegram = telegramOf(doc);
  if (isTelegram && !telegram) return [];
  return [{
    id: doc.document_id,
    title: doc.title,
    sourceType,
    author: asString(telegram?.channel_label),
    channelLabel: asString(telegram?.channel_label),
    messageId: asString(telegram?.message_id),
    excerpt: doc.excerpt,
    timestamp: doc.published_at || doc.observed_at,
    sourceId: doc.source_id,
    documentVersionId: doc.document_version_id,
    publishedAt: telegram?.published_at ?? doc.published_at,
    collectedAt: doc.observed_at,
    editedAt: telegram?.edited_at ?? null,
    media: telegram?.media ?? [],
    imageUrl: doc.image_url ?? null,
    url: safeFeedUrl(doc.canonical_url, isTelegram),
    read: !!doc.read_at,
    bookmarked: !!doc.bookmarked_at,
    versionNumber: doc.version_number,
  }];
}

/** Transforms raw documents into standard feed items. */
export function documentsToItems(docs: FeedDocument[], isTelegram: boolean, sourceType: FeedStreamItem['sourceType']): FeedStreamItem[] {
  return docs.flatMap((doc) => documentToItem(doc, isTelegram, sourceType));
}

/** Client-side filter state applied on top of the server page(s). */
export interface FeedClientFilters {
  hiddenIds: ReadonlySet<string>;
  onlyUnread: boolean;
  sourceFilter: string;
  /** Trimmed lower-case search text. */
  needle: string;
}

/** Items that survive hiding, unread-only, source and text filters. */
export function filterFeedItems(items: FeedStreamItem[], filters: FeedClientFilters): FeedStreamItem[] {
  const { hiddenIds, onlyUnread, sourceFilter, needle } = filters;
  return items.filter((i) =>
    !(i.documentVersionId && hiddenIds.has(i.documentVersionId))
    && (!onlyUnread || !i.read)
    && (sourceFilter === 'all' || i.sourceId === sourceFilter)
    && (!needle || `${i.title} ${i.excerpt ?? ''}`.toLowerCase().includes(needle)),
  );
}

/** Newest collection timestamp across items, or null when none has one. */
export function newestCollectedAt(items: FeedStreamItem[]): string | null {
  return items.reduce<string | null>((max, i) => (i.collectedAt && (!max || i.collectedAt > max) ? i.collectedAt : max), null);
}

/** Selected Telegram identities whose version is no longer the current one in the loaded items. */
export function staleSelections(selected: SelectedTelegramIdentity[], items: FeedStreamItem[]): SelectedTelegramIdentity[] {
  const current = new Map(items.map((item) => [item.id, item]));
  return selected.filter((item) => current.get(item.documentId)?.documentVersionId !== item.documentVersionId);
}
