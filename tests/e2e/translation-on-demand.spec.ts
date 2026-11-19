import type { Page } from '@playwright/test';
import { test, expect, expectNoRawKeys } from './fixtures';
import { collectedRecordCount, escapeRe, findSources } from './sources-helpers';
import { uiText } from './ui-text';

/**
 * On-demand content translation end-to-end. Nothing may be translated until the owner clicks a Translate control
 * (no POST /api/v1/translations/batches on load or on opening a detail dialog). The fake model may not support
 * translation, so a clicked item may settle as a translated row (flip control) or as a reason note; a spinner that
 * never ends is the failure. Needs outbound public network for the BBC World built-in (same as dashboard-ux).
 */
const BBC = 'BBC World News';
const FEED_NAME = 'e2e-translation feed';
const BATCHES = '/api/v1/translations/batches';
const SETTLE_MS = 75_000;

/** Per-item Translate button: named `Translate “<title>”` when the row has a title, plain `Translate` otherwise (header control is `Translate: XX`). */
const itemButton = (scope: Page | ReturnType<Page['getByRole']>) =>
  scope.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('translations', 'translate'))}( “.*”)?$`) });
const TRANSLATING = new RegExp(escapeRe(uiText('translations', 'translating')));
const ALL_REASONS = ['note.unchanged', 'note.blocked', 'note.failed', 'reason.privacyBlocked', 'reason.modelCapabilityMissing', 'reason.inputTooLarge', 'reason.deadlineExceeded', 'reason.invalidModelOutput', 'reason.attemptsExhausted', 'reason.resourceUnavailable', 'reason.staleRequest', 'reason.translationDisabled', 'reason.rateLimited', 'reason.requestFailed'];
const REASON_RE = new RegExp(ALL_REASONS.map((k) => escapeRe(uiText('translations', k))).join('|'));
const controlButton = (page: Page) => page.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('translations', 'control.label'))}: `) });

let dashboardUrl = '';
const counts = { case1: -1, case3: -1, case4: -1, case4Hydration: -1, case5Sizes: [] as number[] };

/** Records POST /translations/batches (item counts) and GET /translations/batches/{id} calls made by `page`. */
function watchBatches(page: Page) {
  const posts: number[] = [];
  const gets: string[] = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (url.pathname === BATCHES && req.method() === 'POST') posts.push((req.postDataJSON() as { items: unknown[] }).items.length);
    else if (url.pathname.startsWith(`${BATCHES}/`) && req.method() === 'GET') gets.push(url.pathname);
  });
  return { posts, gets };
}

/** Owner PATCH of the workspace translation setting (same endpoint Settings > Translation uses). */
async function setTranslation(page: Page, enabled: boolean) {
  const session = (await (await page.request.get('/api/v1/auth/session')).json()) as { csrfToken: string };
  const origin = new URL((await page.request.get('/api/v1/auth/session')).url()).origin;
  const cur = (await (await page.request.get('/api/v1/settings/translation')).json()) as { configuration_revision: number; target_language: string };
  const res = await page.request.patch('/api/v1/settings/translation', {
    headers: { 'X-CSRF-Token': session.csrfToken, Origin: origin },
    data: { enabled, target_language: cur.target_language, expected_revision: cur.configuration_revision },
  });
  expect(res.ok(), `set translation enabled=${enabled}`).toBe(true);
}

/** Opens the shared dashboard and waits for the shell to settle. */
async function openDash(page: Page) {
  expect(dashboardUrl, 'setup test created a dashboard').toBeTruthy();
  await page.goto(dashboardUrl);
  await expect(page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') })).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
}

/** Waits until no "Translating…" spinner remains in `scope`, failing the test if one never settles. */
async function expectNotStuck(page: Page) {
  await expect(page.getByRole('button', { name: TRANSLATING })).toHaveCount(0, { timeout: SETTLE_MS });
}

test.describe.configure({ mode: 'serial', timeout: 300_000 });

test.describe('translation on demand', () => {
  test('setup: BBC news dashboard with stories, translation enabled', async ({ page }) => {
    await page.goto('/dashboard');
    const create = page.getByRole('button', { name: uiText('dashboard', 'createFirstDashboard') });
    if (await create.waitFor({ timeout: 20_000 }).then(() => true, () => false)) {
      await create.click();
      await page.getByLabel(uiText('dashboard', 'dashboardName')).fill('e2e-translation home');
      await page.getByRole('dialog').getByRole('button', { name: uiText('dashboard', 'create'), exact: true }).click();
    }
    await page.getByRole('button', { name: uiText('dashboard', 'setup.connect') }).click();
    const wizard = page.getByRole('dialog', { name: uiText('sources', 'starter.title') });
    await expect(wizard).toBeVisible();
    const boxes = wizard.getByRole('checkbox');
    await expect(boxes.first()).toBeVisible({ timeout: 30_000 });
    for (const box of await boxes.all()) {
      const label = (await box.getAttribute('aria-label')) ?? '';
      const keep = label === uiText('sources', 'starter.itemSelect', { name: BBC });
      if ((await box.isChecked()) !== keep) await box.setChecked(keep);
    }
    await wizard.getByRole('button', { name: uiText('sources', 'next'), exact: true }).click();
    const rowDone = new RegExp(`${escapeRe(uiText('sources', 'starter.statusDone'))}|${escapeRe(uiText('sources', 'starter.statusExists'))}`, 'i');
    await expect(wizard.getByTestId('starter-row-bbc_world')).toContainText(rowDone, { timeout: 120_000 });
    await wizard.getByRole('button', { name: uiText('sources', 'buildDashboard') }).click();
    const picker = page.getByRole('dialog').filter({ hasText: uiText('dashboard', 'applyPreset') });
    await expect(picker).toBeVisible();
    await picker.getByRole('button', { name: uiText('dashboard', 'applyPreset') }).click();
    await expect(picker).toBeHidden({ timeout: 60_000 });
    await expect(page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') })).toBeEnabled({ timeout: 30_000 });
    dashboardUrl = new URL(page.url()).pathname;

    // The preset's Overview tab has no feed; add one bound to BBC so item Translate buttons exist.
    await page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') }).click();
    await page.getByRole('button', { name: uiText('dashboard', 'addGadget.label'), exact: true }).click();
    const gadget = page.getByRole('dialog', { name: uiText('dashboard', 'addGadget.title') });
    await gadget.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('gadgetSettings', 'templateTypes.feed.name'))}\\b`) }).click();
    await gadget.getByRole('checkbox', { name: new RegExp(`^${escapeRe(BBC)}`) }).click();
    await gadget.getByLabel(uiText('gadgetSettings', 'name'), { exact: true }).fill(FEED_NAME);
    const add = gadget.getByRole('button', { name: uiText('dashboard', 'addGadget.label'), exact: true });
    await expect(add).toBeEnabled({ timeout: 120_000 });
    await add.click();
    await expect(gadget).toBeHidden({ timeout: 60_000 });
    const save = page.getByRole('button', { name: uiText('dashboard', 'saveLayout') });
    if (await save.isEnabled()) await save.click();
    await page.getByRole('button', { name: uiText('dashboard', 'finishEditing') }).click();

    const [source] = await findSources(page, 'BBC');
    expect(source, 'BBC source exists').toBeTruthy();
    expect(await collectedRecordCount(page, source!.id, 120_000), 'BBC records').toBeGreaterThan(0);
    await expect
      .poll(async () => ((await (await page.request.get(`/api/v1/stories?limit=50&source_ids=${source!.id}`)).json()) as { items: unknown[] }).items.length, { timeout: 180_000, message: 'stories clustered' })
      .toBeGreaterThan(0);
    await setTranslation(page, true);
  });

  test('1: no translation request on load or when opening an item detail', async ({ page }) => {
    const { posts } = watchBatches(page);
    await openDash(page);
    await page.waitForLoadState('networkidle');
    await expect(itemButton(page).first()).toBeAttached({ timeout: 60_000 });
    await page.waitForTimeout(5_000);
    expect(posts, 'POST batches after load').toEqual([]);
    // Open an item detail: the feed row title links to the document page (story rows open a dialog instead).
    const title = page.locator('a[href^="/knowledge/documents/"]').first();
    await expect(title).toBeVisible({ timeout: 30_000 });
    await title.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(5_000);
    counts.case1 = posts.length;
    expect(posts, 'POST batches after opening item detail').toEqual([]);
    await expectNoRawKeys(page);
  });

  test('2: header control shows state; toggling off hides Translate buttons, on restores them', async ({ page }) => {
    await openDash(page);
    const ctl = controlButton(page);
    await expect(ctl).toBeVisible({ timeout: 30_000 });
    await expect(ctl).toContainText(/: (VI|EN)$/);
    await expect(itemButton(page).first()).toBeAttached({ timeout: 60_000 });
    await ctl.click();
    const sw = page.getByRole('switch', { name: uiText('translations', 'enable') });
    await expect(sw).toBeChecked();
    await expect(sw).toBeEnabled({ timeout: 15_000 });
    await sw.click();
    await expect(ctl).toContainText(uiText('translations', 'control.off'), { timeout: 15_000 });
    await expect(itemButton(page)).toHaveCount(0, { timeout: 15_000 });
    await expect(sw).toBeEnabled({ timeout: 15_000 });
    await sw.click();
    await expect(ctl).toContainText(/: (VI|EN)$/, { timeout: 15_000 });
    await expect(itemButton(page).first()).toBeAttached({ timeout: 15_000 });
    await expectNoRawKeys(page);
  });

  test('3 and 4: one Translate click posts one item and settles; repeat after reload settles', async ({ page }) => {
    const first = watchBatches(page);
    await openDash(page);
    const btn = itemButton(page).first();
    await expect(btn).toBeAttached({ timeout: 60_000 });
    await btn.scrollIntoViewIfNeeded();
    await btn.click();
    await expect.poll(() => first.posts.length, { timeout: 30_000 }).toBeGreaterThan(0);
    await expectNotStuck(page);
    counts.case3 = first.posts.length;
    expect(first.posts, 'exactly one POST with one item').toEqual([1]);
    const flip = page.getByRole('button', { name: new RegExp(`^(${escapeRe(uiText('translations', 'originalShort'))}|${escapeRe(uiText('translations', 'translationShort'))})$`) });
    const settled = (await flip.count()) > 0 || REASON_RE.test(await page.locator('body').innerText());
    expect(settled, 'item shows translated flip control or a reason note').toBe(true);

    // Reload and translate the same item again (server cache / idempotent path).
    const second = watchBatches(page);
    await page.reload();
    await expect(page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') })).toBeEnabled({ timeout: 30_000 });
    const again = itemButton(page).first(); // same first row after reload (nothing is translated yet)
    await expect(again).toBeAttached({ timeout: 60_000 });
    await again.click();
    await expect.poll(() => second.posts.length, { timeout: 30_000 }).toBeGreaterThan(0);
    await expectNotStuck(page);
    counts.case4 = second.posts.length;
    counts.case4Hydration = second.gets.length;
    expect(second.posts, 'one POST after reload').toEqual([1]);
  });

  test('5: Translate all sends batches of at most 25', async ({ page }) => {
    const { posts } = watchBatches(page);
    await openDash(page);
    const all = page.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('translations', 'translateAll'))}$`) }).first();
    await expect(all).toBeVisible({ timeout: 60_000 });
    await all.click();
    await expect.poll(() => posts.length, { timeout: 30_000 }).toBeGreaterThan(0);
    await expectNotStuck(page);
    counts.case5Sizes = [...posts];
    for (const size of posts) expect(size).toBeLessThanOrEqual(25);
  });

  test('6: default and terminal skin screenshots, no horizontal overflow', async ({ page }, testInfo) => {
    for (const skin of ['default', 'terminal'] as const) {
      await page.addInitScript((s) => {
        if (s === 'terminal') localStorage.setItem('umwelt-skin', 'terminal');
        else localStorage.removeItem('umwelt-skin');
      }, skin);
      await openDash(page);
      if (skin === 'terminal') await expect(page.locator('html')).toHaveAttribute('data-skin', 'terminal');
      else await expect(page.locator('html')).not.toHaveAttribute('data-skin', 'terminal');
      await expect(itemButton(page).first()).toBeAttached({ timeout: 60_000 });
      const region = page.getByRole('region').first();
      await region.scrollIntoViewIfNeeded();
      const path = testInfo.outputPath(`dashboard-${skin}.png`);
      await page.screenshot({ path, fullPage: true });
      await testInfo.attach(`dashboard-${skin}`, { path, contentType: 'image/png' });
      const overflow = await page.getByRole('region').evaluateAll((els) => els.filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.getAttribute('aria-label')));
      expect(overflow, `${skin}: gadgets with horizontal overflow`).toEqual([]);
      console.log(`screenshot ${skin}: ${path}`);
    }
    console.log(`translation counts ${JSON.stringify(counts)}`);
  });
});
