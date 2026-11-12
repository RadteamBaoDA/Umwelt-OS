/** Name prefix of the per-workspace live-feed IndexedDB databases (`<prefix><workspaceId>`). */
export const LIVE_FEED_DB_PREFIX = 'umwelt-live-feed:';

/**
 * Delete every `umwelt-live-feed:*` database. Called on logout and account erasure so third-party items fetched
 * for one account do not outlive its session. Best-effort: browsers without `indexedDB.databases()` skip it, and a
 * delete blocked by an open connection resolves immediately instead of holding the erasure path.
 */
export async function clearLiveFeed(): Promise<void> {
  if (typeof indexedDB === 'undefined' || typeof indexedDB.databases !== 'function') return;
  try {
    const names = (await indexedDB.databases())
      .map((db) => db.name)
      .filter((name): name is string => Boolean(name?.startsWith(LIVE_FEED_DB_PREFIX)));
    await Promise.all(
      names.map(
        (name) =>
          new Promise<void>((resolve) => {
            const req = indexedDB.deleteDatabase(name);
            req.onsuccess = req.onerror = req.onblocked = () => resolve();
          }),
      ),
    );
  } catch {
    /* Nothing accessible to clear. */
  }
}
