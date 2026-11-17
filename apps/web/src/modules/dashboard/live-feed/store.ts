import { LIVE_FEED_DB_PREFIX as DB_PREFIX } from '@/core/live-feed-storage';
import type { LiveEvent } from '../live-map-data';

export { clearLiveFeed } from '@/core/live-feed-storage';

/** An event as persisted locally: provider-qualified key, merged tags and the time the browser saw it. */
export type StoredEvent = LiveEvent & { key: string; provider: string; tags: string[]; seen_at: string };

/** Filters applied to stored and proxied events alike. */
export type LiveFilters = { tags: string[]; days: number; query: string; providers: string[] };

const STORE = 'events';
/** Rows older than this are pruned after each write batch. */
export const RETENTION_MS = 30 * 86_400_000;
/** Row cap enforced after each write batch (oldest evicted first). */
export const MAX_ROWS = 5_000;

/** Convert a provider item into a stored row; the provider's tags are merged with the event's own. */
export function toStored(provider: string, providerTags: string[], item: LiveEvent, seenAt: string): StoredEvent {
  const tags = [...new Set([...providerTags, ...(item.tags ?? [])].map((tag) => tag.toLowerCase()))];
  return { ...item, key: `${provider}:${item.id}`, provider, tags, seen_at: seenAt };
}

/** Event time used for ordering and pruning: observation time, else when it was first seen. */
function eventTime(row: { observed_at: string | null; seen_at?: string }): number {
  const parsed = Date.parse(row.observed_at ?? row.seen_at ?? '');
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Keys to delete: rows older than the retention window, then the oldest rows beyond `max`. */
export function pruneKeys(rows: { key: string; observed_at: string | null; seen_at?: string }[], now: number, max = MAX_ROWS): string[] {
  const fresh = rows.filter((row) => eventTime(row) >= now - RETENTION_MS);
  const stale = rows.filter((row) => eventTime(row) < now - RETENTION_MS);
  const overflow = fresh.sort((a, b) => eventTime(b) - eventTime(a)).slice(max);
  return [...stale, ...overflow].map((row) => row.key);
}

/**
 * Keys of a provider's stored rows that its latest full snapshot no longer contains (expired alerts, ended
 * giveaways, past launches). Pure; the caller deletes them in the same transaction as the upsert.
 * An empty batch deletes nothing by default: a 200 that parses to [] (outage, empty proxied page) is indistinguishable
 * from a real empty set, and wiping the store on it would destroy history. Only a caller that has proven the batch
 * is a valid, authoritative empty set (a snapshot source whose payload parsed) may set `allowEmptySnapshot`.
 * @param existingKeys Keys currently stored for the provider.
 * @param newKeys Keys in the provider's new batch.
 * @param allowEmptySnapshot True when an empty `newKeys` is a verified empty current set, so every stored key is stale.
 * @returns Stored keys absent from the batch, or [] when the batch is empty and not authoritative.
 */
export function staleKeys(existingKeys: readonly string[], newKeys: Iterable<string>, allowEmptySnapshot = false): string[] {
  const keep = new Set(newKeys);
  if (!keep.size && !allowEmptySnapshot) return [];
  return existingKeys.filter((key) => !keep.has(key));
}

/**
 * Filter by provider allowlist, period, any-of tags and a case-insensitive text query, newest first.
 * Events without a usable timestamp pass the period filter (same rule as the proxied list). Ordering uses
 * `min(eventTime, now)` so future-dated items (scheduled launches) rank as "now" instead of pinning the top.
 */
export function filterEvents<T extends LiveEvent & { provider: string; tags?: string[] }>(
  rows: T[],
  filters: LiveFilters,
  now: number,
): T[] {
  const cutoff = now - filters.days * 86_400_000;
  const query = filters.query.trim().toLowerCase();
  return rows
    .filter((row) => filters.providers.includes(row.provider))
    .filter((row) => !row.observed_at || !Number.isFinite(Date.parse(row.observed_at)) || Date.parse(row.observed_at) >= cutoff)
    .filter((row) => !filters.tags.length || filters.tags.some((tag) => row.tags?.includes(tag)))
    .filter((row) => !query || `${row.title} ${row.source} ${row.tags?.join(' ') ?? ''}`.toLowerCase().includes(query))
    .sort((a, b) => Math.min(eventTime(b), now) - Math.min(eventTime(a), now));
}

/** Wrap an IDBRequest in a promise. */
function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** Open (creating on first use) the per-workspace database; rejects when IndexedDB is unavailable. */
function openDb(workspaceId: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') return reject(new Error('indexeddb_unavailable'));
    const open = indexedDB.open(`${DB_PREFIX}${workspaceId}`, 1);
    open.onupgradeneeded = () => {
      const store = open.result.createObjectStore(STORE, { keyPath: 'key' });
      store.createIndex('observed_at', 'observed_at');
      store.createIndex('tags', 'tags', { multiEntry: true });
      store.createIndex('provider', 'provider');
    };
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
}

/**
 * Upsert rows by key, then prune by age and count in the same readwrite transaction so readers never see an
 * over-cap store. Resolves after commit. Rejects (callers treat as best-effort) when storage is unavailable or full.
 * @param snapshotProviders Providers whose `rows` are their complete current set: their stored rows absent from
 * `rows` are deleted in the same transaction. Pass only providers whose fetch succeeded and whose source is a true
 * snapshot (see `snapshotScope` in sources.ts); a windowed or capped source must never be listed.
 * @param allowEmptySnapshot Set only by the worker after a snapshot source's payload parsed successfully: then a
 * listed provider with zero rows really has no current items, and all its stored rows are deleted. Proxied callers
 * leave it false, so an empty batch there never prunes (see `staleKeys`).
 */
export async function upsertEvents(
  workspaceId: string,
  rows: StoredEvent[],
  now = Date.now(),
  snapshotProviders: readonly string[] = [],
  allowEmptySnapshot = false,
): Promise<void> {
  const db = await openDb(workspaceId);
  try {
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const done = new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    for (const row of rows) store.put(row);
    for (const provider of snapshotProviders) {
      const existing = (await request(store.index('provider').getAllKeys(provider))) as string[];
      const fresh = rows.filter((row) => row.provider === provider).map((row) => row.key);
      for (const key of staleKeys(existing, fresh, allowEmptySnapshot)) store.delete(key);
    }
    const all = (await request(store.getAll())) as StoredEvent[];
    for (const key of pruneKeys(all, now)) store.delete(key);
    await done;
  } finally {
    db.close();
  }
}

/** Read every stored row of a workspace (at most MAX_ROWS); an unavailable store reads as empty. */
export async function loadEvents(workspaceId: string): Promise<StoredEvent[]> {
  try {
    const db = await openDb(workspaceId);
    try {
      return (await request(db.transaction(STORE).objectStore(STORE).getAll())) as StoredEvent[];
    } finally {
      db.close();
    }
  } catch {
    return [];
  }
}
