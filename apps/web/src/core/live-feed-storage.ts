/** Name prefix of the per-workspace live-feed IndexedDB databases (`<prefix><workspaceId>`). */
export const LIVE_FEED_DB_PREFIX = 'umwelt-live-feed:';

/**
 * Delete every `umwelt-live-feed:*` database. Called on logout and account erasure so third-party items fetched
 * for one account do not outlive its session. Callers await it before navigating, bounded by `CLEAR_TIMEOUT_MS`.
 * Best-effort: browsers without `indexedDB.databases()` skip it. A delete blocked by an open connection resolves
 * on `onblocked` (the request stays queued and completes once the connection closes, e.g. on navigation).
 */
export async function clearLiveFeed(): Promise<void> {
  // Bound the wait so a stuck delete can never hang logout; the delete still completes in the browser.
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, CLEAR_TIMEOUT_MS);
  });
  try {
    await Promise.race([deleteAll(), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Longest `clearLiveFeed` waits for the deletes before letting its caller navigate. */
export const CLEAR_TIMEOUT_MS = 1500;

/** Delete every live-feed database; resolves per database on success, error or `onblocked`. */
async function deleteAll(): Promise<void> {
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
