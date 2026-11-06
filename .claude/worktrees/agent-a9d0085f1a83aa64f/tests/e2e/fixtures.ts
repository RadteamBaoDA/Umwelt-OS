import { test as base, expect } from '@playwright/test';

/**
 * Owner-session test (storageState comes from the `system` project in playwright.config.ts). The saved CSRF cookie is
 * usually stale because earlier tests rotated it, and every new context replays it. The server rotates a non-current
 * cookie on the next session read (under the NOWAIT owner lock, which can answer 409 under contention), so one
 * sequential session read first leaves the cookie current and the page's own session reads stay read-only. A 409 is
 * the server's documented "retry" answer (background workers can hold the owner lock), so it is retried like the app's
 * `retrySessionConflict`. The assertion also fails fast and clearly if the owner storage state was not applied.
 */
export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    let r = await page.request.get('/api/v1/auth/session');
    for (let attempt = 1; r.status() === 409 && attempt < 6; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
      r = await page.request.get('/api/v1/auth/session');
    }
    expect(r.status(), 'owner storageState not applied').toBe(200);
    // Persist the now-current CSRF cookie so the next test's context replays it and takes the lock-free read path:
    // with many sources collecting, overlapping FOR SHARE admissions starve the rotation's NOWAIT owner lock.
    const state = testInfo.project.use.storageState;
    if (typeof state === 'string') await page.context().storageState({ path: state });
    await use(page);
    // The app may rotate the cookie again during the test; keep the latest one for the next test.
    if (typeof state === 'string') await page.context().storageState({ path: state }).catch(() => undefined);
  },
});
export { expect };
