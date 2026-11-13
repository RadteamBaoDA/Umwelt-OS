import { test, expect, expectNoRawKeys, E2E_LOCALE } from './fixtures';

/**
 * Locale-agnostic sweep of the main screens: each must render without a raw message key in its visible text. The
 * `i18nMissingGuard` auto fixture additionally fails on any `[i18n-missing]` console line. Run it once per locale
 * (`E2E_LOCALE=vi-vi`) against a web image built with NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N=1.
 */
const SCREENS = ['/dashboard', '/chat', '/settings/global', '/settings/sources', '/settings/models', '/knowledge/entities', '/news', '/timeline'];

test(`landing renders without raw keys (${E2E_LOCALE})`, async ({ page }) => {
  await page.goto(`/?lang=${E2E_LOCALE}`);
  await expect(page.locator('main, body').first()).toBeVisible();
  await expectNoRawKeys(page);
});

for (const path of SCREENS) {
  test(`${path} renders without raw keys (${E2E_LOCALE})`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState('networkidle');
    await expectNoRawKeys(page);
  });
}
