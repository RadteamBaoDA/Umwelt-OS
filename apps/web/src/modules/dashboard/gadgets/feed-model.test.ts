import { describe, expect, it } from 'vitest';
import {
  defaultSourceTypeFor, documentsToItems, filterFeedItems, newestCollectedAt, safeFeedUrl, staleSelections,
  type FeedDocument, type FeedStreamItem,
} from './feed-model';

const doc = (over: Partial<FeedDocument> = {}): FeedDocument => ({
  document_id: 'd1', title: 'Title', excerpt: 'Body', published_at: '2026-01-02T00:00:00Z', observed_at: '2026-01-03T00:00:00Z',
  source_id: 's1', document_version_id: 'v1', canonical_url: 'https://example.com/a', read_at: null, bookmarked_at: null, version_number: 2,
  provider_metadata: null, ...over,
}) as FeedDocument;
const item = (over: Partial<FeedStreamItem> = {}): FeedStreamItem => ({ id: 'i', title: 'T', sourceType: 'news', timestamp: 't', ...over });

describe('feed model', () => {
  it('restricts links to web schemes, and Telegram links to https t.me', () => {
    expect(safeFeedUrl('https://example.com/x', false)).toBe('https://example.com/x');
    expect(safeFeedUrl('javascript:alert(1)', false)).toBeNull();
    expect(safeFeedUrl('http://t.me/x', true)).toBeNull();
    expect(safeFeedUrl('https://evil.com/x', true)).toBeNull();
    expect(safeFeedUrl('https://t.me/x', true)).toBe('https://t.me/x');
    expect(safeFeedUrl(null, false)).toBeNull();
    expect(safeFeedUrl('not a url', false)).toBeNull();
  });

  it('chooses the item source type from the renderer', () => {
    expect(defaultSourceTypeFor('telegram_feed')).toBe('telegram');
    expect(defaultSourceTypeFor('news_feed')).toBe('news');
    expect(defaultSourceTypeFor('feed')).toBe('document');
  });

  it('maps documents to items and drops non-Telegram documents from a Telegram feed', () => {
    const telegramDoc = doc({ provider_metadata: { provider: 'telegram', telegram: { channel_label: 'Chan', message_id: '7', published_at: 'p', edited_at: null, media: [] } } as unknown as FeedDocument['provider_metadata'] });
    const [mapped] = documentsToItems([telegramDoc], true, 'telegram');
    expect(mapped).toMatchObject({ id: 'd1', channelLabel: 'Chan', messageId: '7', url: null, read: false, versionNumber: 2, timestamp: '2026-01-02T00:00:00Z' });
    expect(documentsToItems([doc()], true, 'telegram')).toEqual([]);
    expect(documentsToItems([doc({ read_at: 'x', published_at: null })], false, 'document')[0]).toMatchObject({ read: true, timestamp: '2026-01-03T00:00:00Z', url: 'https://example.com/a' });
  });

  it('filters by hidden version, unread, source and text', () => {
    const items = [item({ id: 'a', title: 'Alpha', documentVersionId: 'v1', sourceId: 's1', read: true }), item({ id: 'b', title: 'Beta', excerpt: 'gamma', sourceId: 's2' })];
    const base = { hiddenIds: new Set<string>(), onlyUnread: false, sourceFilter: 'all', needle: '' };
    expect(filterFeedItems(items, base)).toHaveLength(2);
    expect(filterFeedItems(items, { ...base, hiddenIds: new Set(['v1']) }).map((i) => i.id)).toEqual(['b']);
    expect(filterFeedItems(items, { ...base, onlyUnread: true }).map((i) => i.id)).toEqual(['b']);
    expect(filterFeedItems(items, { ...base, sourceFilter: 's1' }).map((i) => i.id)).toEqual(['a']);
    expect(filterFeedItems(items, { ...base, needle: 'gamma' }).map((i) => i.id)).toEqual(['b']);
  });

  it('finds the newest collection time and stale selections', () => {
    expect(newestCollectedAt([item({ collectedAt: '2026-01-01' }), item({ collectedAt: '2026-02-01' }), item()])).toBe('2026-02-01');
    expect(newestCollectedAt([item()])).toBeNull();
    const selected = [{ sourceId: 's', documentId: 'a', documentVersionId: 'v1', versionNumber: 1 }, { sourceId: 's', documentId: 'b', documentVersionId: 'v9', versionNumber: 1 }];
    expect(staleSelections(selected, [item({ id: 'a', documentVersionId: 'v1' }), item({ id: 'b', documentVersionId: 'v2' })]).map((s) => s.documentId)).toEqual(['b']);
  });
});
