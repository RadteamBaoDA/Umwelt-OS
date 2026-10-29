import type { Locator, Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { acknowledgeTerms, deleteSources, findSources, openWizard, waitForFetchOutcome, writeHeaders } from './sources-helpers';

/**
 * Credential-gated datasources the disposable stack has no owner account for: Google (Gmail, Calendar, Drive) and
 * Reddit (OAuth2), GitHub (GitHub App OAuth), Telegram (bot token) and X (bearer header). For each one the owner must be
 * able to start adding it through the real wizard, and without valid credentials the app must fail cleanly: a specific
 * readable error (or a visibly blocked step), no uncaught page error, no console error, no 5xx other than the documented
 * 503 "authorization is unavailable" of an OAuth start, and the half-created source must be discardable.
 *
 * Official public demo keys: none of the providers here publishes one (checked against each provider's own docs).
 */
const PREFIX = 'e2e-cred';

/** Collects the signals that make a failure "unclean": page errors, console errors, 5xx answers. */
function watch(page: Page) {
  const bad: string[] = [];
  const statuses: string[] = [];
  page.on('pageerror', (e) => bad.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    // The browser logs every non-2xx fetch as "Failed to load resource"; statuses are judged on responses below.
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) bad.push(`console: ${m.text()}`);
  });
  page.on('response', (r) => {
    const path = new URL(r.url()).pathname;
    if (r.status() >= 400 && path.startsWith('/api/')) statuses.push(`${r.status()} ${r.request().method()} ${path}`);
    // 503 is the documented answer of an OAuth start when no OAuth client is configured; anything else >= 500 is a defect.
    if (r.status() >= 500 && !(r.status() === 503 && /\/oauth\/.+\/start$/.test(path))) bad.push(`http ${r.status()} ${path}`);
  });
  return { bad, statuses };
}

/** Opens the wizard, picks the provider card, names the draft and advances to the Connect step (creates the draft). */
async function startAdding(page: Page, card: RegExp, name: string) {
  const dialog = await openWizard(page);
  await dialog.getByRole('button', { name: card }).click();
  await dialog.getByLabel('Source name').fill(name);
  const next = dialog.getByRole('button', { name: 'Next', exact: true });
  await next.click();
  await expect(dialog.getByRole('heading', { name: /Credentials|Connect/i }).first().or(dialog.getByText('1. Connect').first())).toBeVisible();
  return { dialog, next };
}

/** Closes the wizard through its X and confirms discarding the unverified draft, as an owner would. */
async function discardDraft(page: Page, dialog: Locator, name: string) {
  await dialog.getByRole('button', { name: 'Close' }).first().click();
  await page.getByRole('button', { name: 'Discard source' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.waitForTimeout(500);
  const left = (await findSources(page, name)).filter((s) => s.status !== 'archived');
  expect(left.map((x) => `${x.name}:${x.status}`), 'discarded draft must not stay active').toEqual([]);
}

/**
 * OAuth providers. The wizard creates the draft and shows the Connect step. Connecting needs a saved scope revision
 * that the wizard only collects on a later step, so the Connect button stays disabled on a fresh draft (reported as a
 * wizard design gap, logged as STATE). The OAuth start endpoint itself must answer the documented 503 "unavailable"
 * when no OAuth client is configured, and the draft must be discardable.
 */
const OAUTH_CASES = [
  { id: 'google_mail', card: /^Gmail/, path: 'oauth/google/google_mail/start', detail: /Google connector authorization is unavailable/ },
  { id: 'google_calendar', card: /^Google Calendar/, path: 'oauth/google/google_calendar/start', detail: /Google connector authorization is unavailable/ },
  { id: 'google_drive', card: /^Google Drive/, path: 'oauth/google/google_drive/start', detail: /Google connector authorization is unavailable/ },
  { id: 'reddit', card: /^Reddit/, path: 'oauth/reddit/start', detail: /Reddit connector authorization is unavailable/ },
];

for (const c of OAUTH_CASES) {
  test(`${c.id}: owner starts adding; OAuth without a configured client fails cleanly`, async ({ page }) => {
  test.setTimeout(120_000);
    const name = `${PREFIX} ${c.id} ${Date.now()}`;
    const { bad, statuses } = watch(page);
    try {
      const { dialog } = await startAdding(page, c.card, name);
      // Draft creation and the follow-up configuration reads must not fail (regression: oauth2 auth_method answered 500).
      await expect(dialog.getByRole('alert')).toHaveCount(0);
      await expect(dialog.getByRole('button', { name: /^Connect (Google|Reddit)$/ })).toBeVisible();
      await page.waitForTimeout(1500); // let the editor's configuration/status queries settle
      console.log(`STATE ${c.id} :: connect enabled=${await dialog.getByRole('button', { name: /^Connect (Google|Reddit)$/ }).isEnabled()}`);
      const [source] = await findSources(page, name);
      expect(source, 'draft source exists').toBeTruthy();
      const res = await page.request.post(`/api/v1/connectors/${source.id}/${c.path}`, {
        headers: await writeHeaders(page), data: { expected_source_generation: 1, expected_revision: 1 },
      });
      const body = await res.text();
      console.log(`RESULT ${c.id} :: ${res.status()} ${body}`);
      expect(res.status()).toBe(503);
      expect(body).toMatch(c.detail);
      expect(bad, 'clean failure').toEqual([]);
      console.log(`STATUSES ${c.id} :: ${statuses.join(', ')}`);
      await discardDraft(page, dialog, name);
    } finally {
      await deleteSources(page, name);
    }
  });
}

test('github: owner starts adding; blocked Connect step explains with a readable error', async ({ page }) => {
  test.setTimeout(120_000);
  const name = `${PREFIX} github ${Date.now()}`;
  const { bad, statuses } = watch(page);
  try {
    const { dialog, next } = await startAdding(page, /^GitHub Credentials required/, name);
    const connect = dialog.getByRole('button', { name: /Connect GitHub/i });
    await expect(connect).toBeVisible();
    console.log(`STATE github :: connect enabled=${await connect.isEnabled()}`);
    await next.click(); // saves the draft revision, which needs a scope the wizard asks for only on the next step
    const alert = dialog.getByRole('alert').first();
    await expect(alert).toBeVisible({ timeout: 30_000 });
    const text = (await alert.innerText()).trim();
    console.log(`RESULT github :: ${text} :: ${statuses.join(', ')}`);
    expect(text).toMatch(/configuration is invalid/i);
    expect(bad, 'clean failure').toEqual([]);
    await discardDraft(page, dialog, name);
  } finally {
    await deleteSources(page, name);
  }
});

test('telegram: dummy bot token is rejected with a readable error', async ({ page }) => {
  test.setTimeout(120_000);
  const name = `${PREFIX} telegram ${Date.now()}`;
  const { bad, statuses } = watch(page);
  try {
    const { dialog, next } = await startAdding(page, /^Telegram channels/, name);
    await page.locator('#source-telegram-token').fill('123456:dummy-e2e-token');
    await expect(next).toBeEnabled();
    await next.click();
    await page.locator('#source-scope-telegram_chat_ids').fill('-1001234567890');
    await acknowledgeTerms(dialog);
    await dialog.getByRole('button', { name: 'Validate draft' }).click();
    const alert = dialog.getByRole('alert').first();
    await expect(alert).toBeVisible({ timeout: 60_000 });
    const text = (await alert.innerText()).trim();
    console.log(`RESULT telegram :: ${text} :: ${statuses.join(', ')}`);
    expect(text.length, 'error text is specific').toBeGreaterThan(8);
    expect(text).not.toMatch(/500|internal server error|undefined|\[object/i);
    expect(bad, 'clean failure').toEqual([]);
    await discardDraft(page, dialog, name);
  } finally {
    await deleteSources(page, name);
  }
});

test('x: bearer token wizard reaches validation and fails with a readable error', async ({ page }) => {
  test.setTimeout(120_000);
  const name = `${PREFIX} x ${Date.now()}`;
  const { bad, statuses } = watch(page);
  try {
    const { dialog, next } = await startAdding(page, /^X(?![A-Za-z])/, name);
    await page.locator('#source-local-bearer-token').fill('dummy-e2e-bearer-token');
    await expect(next).toBeEnabled();
    await next.click(); // connect -> choose
    await page.locator('#source-scope-x_account').fill('XDevelopers');
    await acknowledgeTerms(dialog);
    const validated = page.waitForResponse((r) => r.url().includes('/validate-draft') && r.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Validate draft' }).click();
    expect((await validated).status(), 'validate-draft response').toBeLessThan(500);
    await next.click(); // choose -> schedule
    await next.click(); // schedule -> save and test
    await dialog.getByRole('button', { name: 'Save and test fetch' }).click();
    const outcome = await waitForFetchOutcome(dialog, 120_000, 0);
    const text = outcome.text;
    console.log(`RESULT x :: ${text} :: ${statuses.join(', ')}`);
    expect(outcome.ok, 'dummy bearer must not succeed').toBe(false);
    expect(text).toMatch(/Test fetch failed: (invalid_credential|provider_rejected|provider_unauthorized|provider_response_invalid|provider_unavailable|provider_forbidden)\./);
    expect(bad, 'clean failure').toEqual([]);
    await discardDraft(page, dialog, name);
  } finally {
    await deleteSources(page, name);
  }
});
