import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { collectedRecordCount, deleteSources, escapeRe, findSources, openWizard } from './sources-helpers';
import { uiPattern, uiRe, uiText } from './ui-text';

/**
 * Dashboard UX end-to-end (first-run starter packs, preset picker, page frames/templates, feed gadget view/show-more/
 * source tabs, density, map collapse, remote feed images, REST "Test connection"). Tests run in file order in one worker
 * and share the dashboard the first test creates. The live-feed tests need outbound public network access (BBC World,
 * Tuổi Trẻ, hn.algolia.com); use the disposable stack only.
 *
 * UI labels are read from the owner-locale catalogs (`uiText` / `uiRe`, honoring `E2E_LOCALE`), so the same spec runs
 * under en-us and vi-vi. Built-in dataset names and the preset labels from the server are data, not catalog copy, and
 * stay literal.
 */
const FEED_NAME = 'e2e-ux feed';
const BBC = 'BBC World News';
const TUOITRE = 'Tuổi Trẻ News';
const MEDIA_LABEL = uiRe('aiSettings', 'allow.widgetRemoteMedia');

const PAGE = 'e2e-ux columns';
let dashboardUrl = '';

/** Opens the shared dashboard created by the first-run test (optionally on one page tab) and waits for the shell to settle. */
async function openDash(page: Page, tab?: string) {
  expect(dashboardUrl, 'first-run test created a dashboard').toBeTruthy();
  await page.goto(dashboardUrl);
  await expect(page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') })).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
  if (tab) await page.getByRole('tab', { name: tab }).click();
}

/** The feed gadget region added by `adds a two-source feed gadget`. */
const feedRegion = (page: Page) => page.getByRole('region', { name: FEED_NAME, exact: true });

/** Density toggle's accessible name: `<Density>: <Comfortable|Compact>` (dashboard-header builds it the same way). */
const densityName = (mode: 'comfortable' | 'compact') =>
  new RegExp(`^${escapeRe(`${uiText('dashboard', 'density.label')}: ${uiText('dashboard', `density.${mode}`)}`)}`);

/** Sets the "remote widget media" privacy toggle through Settings > Models and saves it. */
async function setRemoteMedia(page: Page, on: boolean) {
  await page.goto('/settings/models');
  const box = page.getByRole('checkbox', { name: MEDIA_LABEL });
  // The privacy checkboxes sit in a collapsed <details>; open it by its summary.
  const privacySummary = new RegExp(escapeRe(uiText('aiSettings', 'privacyTitle')), 'i');
  await page.locator('summary', { hasText: privacySummary }).first().click();
  await expect(box).toBeVisible({ timeout: 30_000 });
  if ((await box.isChecked()) === on) return;
  await box.setChecked(on);
  await page.getByRole('button', { name: uiText('aiSettings', 'save.button') }).click();
  await expect(page.getByText(uiText('aiSettings', 'save.success'))).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await page.locator('summary', { hasText: privacySummary }).first().click();
  expect(await page.getByRole('checkbox', { name: MEDIA_LABEL }).isChecked()).toBe(on);
}

test.describe.configure({ mode: 'serial', timeout: 240_000 });

test.describe('dashboard ux', () => {
  test('first run: starter packs -> build my dashboard -> preset applied', async ({ page }) => {
    await page.goto('/dashboard');
    // A fresh disposable install has no dashboard at all; create the (empty) one a real owner starts with.
    const create = page.getByRole('button', { name: uiText('dashboard', 'createFirstDashboard') });
    if (await create.waitFor({ timeout: 20_000 }).then(() => true, () => false)) {
      await create.click();
      await page.getByLabel(uiText('dashboard', 'dashboardName')).fill('e2e-ux home');
      await page.getByRole('dialog').getByRole('button', { name: uiText('dashboard', 'create'), exact: true }).click();
    }
    // Empty workspace: the first-run panel's source step opens the starter-pack wizard in place.
    await page.getByRole('button', { name: uiText('dashboard', 'setup.connect') }).click();
    const wizard = page.getByRole('dialog', { name: uiText('sources', 'starter.title') });
    await expect(wizard).toBeVisible();
    // Keep only the News pack (two keyless built-ins) so the run stays small.
    const boxes = wizard.getByRole('checkbox');
    await expect(boxes.first()).toBeVisible({ timeout: 30_000 });
    for (const box of await boxes.all()) {
      const label = (await box.getAttribute('aria-label')) ?? '';
      const keep = label === `Add ${BBC}` || label === `Add ${TUOITRE}`;
      if ((await box.isChecked()) !== keep) await box.setChecked(keep);
    }
    await wizard.getByRole('button', { name: uiText('sources', 'next'), exact: true }).click();
    await expect(wizard.getByTestId('starter-row-bbc_world')).toContainText(/Done|Already added/i, { timeout: 120_000 });
    await wizard.getByRole('button', { name: uiText('sources', 'buildDashboard') }).click();
    // The preset picker opens in place with the Monitor preset preselected. "Monitor" is the server's preset label
    // (modules/dashboard/gadgets.py), not catalog copy, so it stays literal in every locale.
    const picker = page.getByRole('dialog').filter({ hasText: uiText('dashboard', 'applyPreset') });
    await expect(picker).toBeVisible();
    await expect(picker.getByRole('button', { name: /^Monitor/, pressed: true })).toBeVisible({ timeout: 30_000 });
    await picker.getByRole('button', { name: uiText('dashboard', 'applyPreset') }).click();
    await expect(picker).toBeHidden({ timeout: 60_000 });
    await expect(page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') })).toBeEnabled({ timeout: 30_000 });
    dashboardUrl = new URL(page.url()).pathname;
    // Applying created a dashboard whose gadgets render (at least one gadget region).
    await expect(page.getByRole('region').first()).toBeVisible({ timeout: 30_000 });
  });

  test('sources page: upload entry and REST "Test connection" tree fills items path', async ({ page }) => {
    await page.goto('/settings/sources');
    await page.getByRole('button', { name: uiText('sources', 'add.datasource') }).click();
    await expect(page.getByRole('menuitem', { name: uiRe('sources', 'menu.upload') })).toBeVisible();
    await page.keyboard.press('Escape');

    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('sources', 'provider.rest'))}`) }).click();
    await dialog.getByLabel(uiText('sources', 'sourceName')).fill('e2e-ux rest');
    await dialog.getByRole('button', { name: uiText('sources', 'next'), exact: true }).click(); // creates the draft source
    await dialog.getByRole('button', { name: uiText('sources', 'next'), exact: true }).click(); // connect -> choose data
    await page.locator('#source-config-url').fill('https://hn.algolia.com/api/v1/search?query=oil&tags=story');
    await dialog.getByRole('button', { name: uiText('sources', 'rest.test') }).click();
    const tree = dialog.getByRole('button', { name: 'hits', exact: true });
    await expect(tree).toBeVisible({ timeout: 60_000 });
    await tree.click();
    await expect(page.locator('#source-items-path')).toHaveValue('hits');
    await deleteSources(page, 'e2e-ux rest');
  });

  test('new page: Three columns frame, then From template News', async ({ page }) => {
    await openDash(page);
    const pages = page.getByRole('tab');
    const before = await pages.count();
    await page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') }).click();
    await page.getByRole('button', { name: uiText('dashboard', 'addGroup.label') }).click();
    const dialog = page.getByRole('dialog', { name: uiText('dashboard', 'addGroup.title') });
    await dialog.getByLabel(uiText('dashboard', 'group.name')).fill(PAGE);
    await dialog.getByRole('radio', { name: uiText('dashboard', 'group.frameThreeColumns') }).check();
    await dialog.getByRole('button', { name: uiText('dashboard', 'create'), exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });
    await expect(page.getByRole('tab', { name: PAGE })).toBeVisible({ timeout: 30_000 });
    expect(await pages.count()).toBeGreaterThan(before);

    // From template -> News opens the preset picker in append mode on the active dashboard.
    await page.getByRole('button', { name: uiText('dashboard', 'addGroup.label') }).click();
    await dialog.getByRole('button', { name: uiText('dashboard', 'group.templateNews'), exact: true }).click();
    const picker = page.getByRole('dialog').filter({ hasText: uiText('dashboard', 'applyPreset') });
    await expect(picker.getByRole('button', { name: uiText('dashboard', 'applyMode.append'), pressed: true })).toBeVisible({ timeout: 30_000 });
    const groupsBefore = await page.getByRole('tab').count();
    await picker.getByRole('button', { name: uiText('dashboard', 'applyPreset') }).click();
    await expect(picker).toBeHidden({ timeout: 60_000 });
    await expect.poll(() => page.getByRole('tab').count(), { timeout: 30_000 }).toBeGreaterThan(groupsBefore);
  });

  test('adds a two-source feed gadget', async ({ page }) => {
    await openDash(page, PAGE);
    await page.getByRole('button', { name: uiText('dashboard', 'toggleEdit') }).click();
    await page.getByRole('button', { name: uiText('dashboard', 'addGadget.label'), exact: true }).click();
    const dialog = page.getByRole('dialog', { name: uiText('dashboard', 'addGadget.title') });
    await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(uiText('gadgetSettings', 'templateTypes.feed.name'))}\\b`) }).click();
    for (const label of [BBC, TUOITRE]) {
      await dialog.getByRole('checkbox', { name: new RegExp(`^${escapeRe(label)}`) }).click();
    }
    await dialog.getByLabel(uiText('gadgetSettings', 'name'), { exact: true }).fill(FEED_NAME);
    const add = dialog.getByRole('button', { name: uiText('dashboard', 'addGadget.label'), exact: true });
    await expect(add).toBeEnabled({ timeout: 120_000 });
    await add.click();
    await expect(dialog).toBeHidden({ timeout: 60_000 });
    const save = page.getByRole('button', { name: uiText('dashboard', 'saveLayout') });
    if (await save.isEnabled()) await save.click();
    await page.getByRole('button', { name: uiText('dashboard', 'finishEditing') }).click();
    // Wait for the live collection so the later tests see items.
    for (const name of ['BBC', 'Tuổi']) {
      const [source] = await findSources(page, name);
      expect(source, `${name} source exists`).toBeTruthy();
      expect(await collectedRecordCount(page, source!.id, 120_000), `${name} collected records`).toBeGreaterThan(0);
      // Normalization runs after the fetch settles; the dashboard projections only list normalized records.
      await expect.poll(async () => ((await (await page.request.get(`/api/v1/documents/dashboard-projections?limit=1&source_ids=${source!.id}`)).json()) as { items: unknown[] }).items.length, { timeout: 120_000 }).toBeGreaterThan(0);
    }
  });

  test('feed gadget UI: source tabs, show more, list/cards persists across reload', async ({ page }) => {
    // Stories are clustered by a background worker after collection; wait until enough exist for "Show N more" (>6 cards).
    const bound = [...(await findSources(page, 'BBC')), ...(await findSources(page, 'Tuổi'))];
    const query = bound.map((source) => `source_ids=${source.id}`).join('&');
    await expect.poll(async () => ((await (await page.request.get(`/api/v1/stories?limit=50&${query}`)).json()) as { items: unknown[] }).items.length, { timeout: 180_000, message: 'stories clustered' }).toBeGreaterThan(6);
    await openDash(page, PAGE);
    const feed = feedRegion(page);
    await expect(feed.getByRole('tablist', { name: uiText('dashboard', 'feed.sourceTabs') })).toBeVisible({ timeout: 15_000 });
    await expect(feed.getByRole('tab')).toHaveCount(3); // All + 2 sources
    const layout = feed.getByRole('group', { name: uiText('dashboard', 'feed.viewLabel') });
    await layout.getByRole('button', { name: uiText('dashboard', 'feed.viewCards') }).click();
    const more = feed.getByRole('button', { name: uiPattern('dashboard', 'showMore', { count: '\\d+' }) });
    await expect(more).toBeVisible({ timeout: 15_000 });
    await more.click();
    await expect(feed.getByRole('button', { name: uiText('dashboard', 'showLess') })).toHaveAttribute('aria-expanded', 'true');
    await openDash(page, PAGE);
    await expect(feedRegion(page).getByRole('group', { name: uiText('dashboard', 'feed.viewLabel') }).getByRole('button', { name: uiText('dashboard', 'feed.viewCards') })).toHaveAttribute('aria-pressed', 'true', { timeout: 15_000 });
  });

  test('feed images: projections carry a signed proxy URL only when remote media is allowed', async ({ page }) => {
    const [bbc] = await findSources(page, 'BBC');
    const projections = async () => {
      const res = await page.request.get(`/api/v1/documents/dashboard-projections?limit=100&source_ids=${bbc!.id}`);
      expect(res.status()).toBe(200);
      return (await res.json() as { items: { image_url?: string | null }[] }).items;
    };
    // Off (default): no document carries an image.
    await setRemoteMedia(page, false);
    const off = await projections();
    expect(off.length, 'BBC documents exist').toBeGreaterThan(0);
    expect(off.filter((i) => i.image_url)).toHaveLength(0);

    // On: BBC carries media:thumbnail, so items expose a same-origin signed proxy path that serves a real image.
    await setRemoteMedia(page, true);
    const on = (await projections()).filter((i) => i.image_url);
    expect(on.length, 'BBC items with a proxied image (needs outbound network + media:thumbnail)').toBeGreaterThan(0);
    expect(on[0]!.image_url).toMatch(/^\/api\/v1\/media\/proxy\?/);
    const image = await page.request.get(on[0]!.image_url!);
    expect(image.status(), 'proxy answers 200').toBe(200);
    expect(image.headers()['content-type'] ?? '').toMatch(/^image\//);
    await setRemoteMedia(page, false);
  });

  test('feed images UI: Cards view renders proxied <img> (only with remote media on)', async ({ page }) => {
    await setRemoteMedia(page, true);
    await openDash(page, PAGE);
    const feed = feedRegion(page);
    await feed.getByRole('group', { name: uiText('dashboard', 'feed.viewLabel') }).getByRole('button', { name: uiText('dashboard', 'feed.viewCards') }).click({ timeout: 15_000 });
    const images = feed.locator('img[src^="/api/v1/media/proxy"]');
    await expect(images.first()).toBeAttached({ timeout: 15_000 });
    await expect.poll(() => images.first().evaluate((img: HTMLImageElement) => img.naturalWidth), { timeout: 15_000 }).toBeGreaterThan(0);
    await setRemoteMedia(page, false);
  });

  test('density and map collapse persist across reload (desktop)', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await openDash(page, uiText('dashboard', 'overview.tab'));
    await page.getByRole('button', { name: densityName('comfortable') }).click();
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
    await page.getByRole('button', { name: densityName('compact') }).click();
    await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact');

    // The map lives on the Overview page (default dashboard group).
    const collapse = page.getByRole('button', { name: uiText('dashboard', 'collapseMap') });
    await expect(collapse).toBeVisible({ timeout: 30_000 });
    await collapse.click();
    await expect(page.getByRole('button', { name: uiText('dashboard', 'expandMap') })).toBeVisible();
    await openDash(page, uiText('dashboard', 'overview.tab'));
    await expect(page.getByRole('button', { name: uiText('dashboard', 'expandMap') })).toBeVisible({ timeout: 30_000 });
    await page.getByRole('button', { name: uiText('dashboard', 'expandMap') }).click();
    await expect(page.getByRole('button', { name: uiText('dashboard', 'collapseMap') })).toBeVisible();
  });
});
