import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test as base, expect, type Page } from '@playwright/test';

/** Locale the owner session runs in (`E2E_LOCALE`, default en-us); anything else is rejected early. */
export const E2E_LOCALE = process.env.E2E_LOCALE ?? 'en-us';
if (E2E_LOCALE !== 'en-us' && E2E_LOCALE !== 'vi-vi') throw new Error(`E2E_LOCALE must be en-us or vi-vi, got ${E2E_LOCALE}`);

/** Console marker emitted by apps/web/src/core/i18n-missing.ts when the web image was built with the detector on. */
const I18N_MISSING_MARKER = '[i18n-missing]';

/** `ns.key` and flat `key` names of every en-us catalog entry, read from the JSON catalogs (not the app bundle). */
function loadCatalogKeys(): Set<string> {
  const dir = join(__dirname, '..', '..', 'apps', 'web', 'locales', 'en-us');
  const keys = new Set<string>();
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const ns = file.replace(/\.json$/, '');
    for (const key of Object.keys(JSON.parse(readFileSync(join(dir, file), 'utf8')) as Record<string, string>)) {
      keys.add(`${ns}.${key}`);
      if (key.includes('.')) keys.add(key);
    }
  }
  return keys;
}

/**
 * Fail when the page's visible text contains a raw message key (`chat.composer.send`), i.e. a token shaped like a dotted
 * key that is present in the flat catalog key set. Catches untranslated/missing keys that next-intl rendered as the key.
 * Reads `body.innerText`, so hidden DOM and attribute values are not checked.
 */
export async function expectNoRawKeys(page: Page): Promise<void> {
  const keys = loadCatalogKeys();
  const text = await page.locator('body').innerText();
  const leaked = [...new Set(text.match(/\b[a-z][A-Za-z0-9]*(\.[a-zA-Z0-9]+){1,8}\b/g) ?? [])].filter((token) => keys.has(token));
  expect(leaked, 'raw i18n keys visible on the page').toEqual([]);
}

/**
 * Switch the owner's stored locale preference to `locale` when it differs. Needs the current CSRF token from the session
 * read and the preference revision; the optimistic-concurrency PUT mirrors what the preferences dialog sends.
 */
async function ensureOwnerLocale(page: Page, csrfToken: string, locale: string): Promise<void> {
  const prefs = await page.request.get('/api/v1/settings/preferences');
  expect(prefs.ok(), 'read owner preferences').toBe(true);
  const current = (await prefs.json()) as { configuration_revision: number; theme: string; locale: string; timezone: string; display_name?: string | null };
  if (current.locale === locale) return;
  const put = await page.request.put('/api/v1/settings/preferences', {
    headers: { 'X-CSRF-Token': csrfToken, Origin: new URL(page.url() === 'about:blank' ? 'http://localhost:3000' : page.url()).origin },
    data: { expected_revision: current.configuration_revision, theme: current.theme, locale, timezone: current.timezone, display_name: current.display_name ?? null },
  });
  expect(put.ok(), `set owner locale ${locale}`).toBe(true);
}

/**
 * Owner-session test (storageState comes from the `system` project in playwright.config.ts). The saved CSRF cookie is
 * usually stale because earlier tests rotated it, and every new context replays it. The server rotates a non-current
 * cookie on the next session read (under the NOWAIT owner lock, which can answer 409 under contention), so one
 * sequential session read first leaves the cookie current and the page's own session reads stay read-only. A 409 is
 * the server's documented "retry" answer (background workers can hold the owner lock), so it is retried like the app's
 * `retrySessionConflict`. The assertion also fails fast and clearly if the owner storage state was not applied.
 */
export const test = base.extend<{ i18nMissingGuard: void }>({
  /**
   * Auto fixture: fails the test when the browser console printed `[i18n-missing] ns.key` (only emitted by a web image
   * built with NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N=1; inert otherwise).
   */
  i18nMissingGuard: [
    async ({ page }, use) => {
      const missing: string[] = [];
      page.on('console', (m) => {
        if (m.text().startsWith(I18N_MISSING_MARKER)) missing.push(m.text());
      });
      await use();
      expect([...new Set(missing)], 'missing i18n keys reported by the app').toEqual([]);
    },
    { auto: true },
  ],
  page: async ({ page }, use, testInfo) => {
    let r = await page.request.get('/api/v1/auth/session');
    for (let attempt = 1; r.status() === 409 && attempt < 6; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
      r = await page.request.get('/api/v1/auth/session');
    }
    expect(r.status(), 'owner storageState not applied').toBe(200);
    if (E2E_LOCALE !== 'en-us') await ensureOwnerLocale(page, ((await r.json()) as { csrfToken: string }).csrfToken, E2E_LOCALE);
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
