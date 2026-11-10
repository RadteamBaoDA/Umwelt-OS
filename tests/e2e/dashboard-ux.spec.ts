import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { collectedRecordCount, deleteSources, escapeRe, findSources, openWizard } from './sources-helpers';

/**
 * Dashboard UX end-to-end (first-run starter packs, preset picker, page frames/templates, feed gadget view/show-more/
 * source tabs, density, map collapse, remote feed images, REST "Test connection"). Tests run in file order in one worker
 * and share the dashboard the first test creates. The live-feed tests need outbound public network access (BBC World,
 * Tuổi Trẻ, hn.algolia.com); use the disposable stack only.
 */
const FEED_NAME = 'e2e-ux feed';
const BBC = 'BBC World News';
const TUOITRE = 'Tuổi Trẻ News';
const MEDIA_LABEL = /Allow widgets to load public images and map tiles/;

const PAGE = 'e2e-ux columns';
let dashboardUrl = '';

/** Opens the shared dashboard created by the first-run test (optionally on one page tab) and waits for the shell to settle. */
async function openDash(page: Page, tab?: string) {
  expect(dashboardUrl, 'first-run test created a dashboard').toBeTruthy();
  await page.goto(dashboardUrl);
  await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
  if (tab) await page.getByRole('tab', { name: tab }).click();
}

/** The feed gadget region added by `adds a two-source feed gadget`. */
const feedRegion = (page: Page) => page.getByRole('region', { name: FEED_NAME, exact: true });

/** Sets the "remote widget media" privacy toggle through Settings > Models and saves it. */
async function setRemoteMedia(page: Page, on: boolean) {
  await page.goto('/settings/models');
  const box = page.getByRole('checkbox', { name: MEDIA_LABEL });
  // The privacy checkboxes sit in a collapsed <details>; open it by its summary.
  await page.locator('summary', { hasText: /privacy/i }).first().click();
  await expect(box).toBeVisible({ timeout: 30_000 });
  if ((await box.isChecked()) === on) return;
  await box.setChecked(on);
  await page.getByRole('button', { name: 'Save AI settings' }).click();
  await expect(page.getByText('AI settings saved.')).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await page.locator('summary', { hasText: /privacy/i }).first().click();
  expect(await page.getByRole('checkbox', { name: MEDIA_LABEL }).isChecked()).toBe(on);
}

test.describe.configure({ mode: 'serial', timeout: 240_000 });

test.describe('dashboard ux', () => {
  test('first run: starter packs -> build my dashboard -> preset applied', async ({ page }) => {
    await page.goto('/dashboard');
    // A fresh disposable install has no dashboard at all; create the (empty) one a real owner starts with.
    const create = page.getByRole('button', { name: 'Create a dashboard' });
    if (await create.waitFor({ timeout: 20_000 }).then(() => true, () => false)) {
      await create.click();
      await page.getByLabel('Dashboard name').fill('e2e-ux home');
      await page.getByRole('dialog').getByRole('button', { name: 'Create', exact: true }).click();
    }
    // Empty workspace: the first-run panel's source step opens the starter-pack wizard in place.
    await page.getByRole('button', { name: 'Connect a source' }).click();
    const wizard = page.getByRole('dialog', { name: 'Starter packs' });
    await expect(wizard).toBeVisible();
    // Keep only the News pack (two keyless built-ins) so the run stays small.
    const boxes = wizard.getByRole('checkbox');
    await expect(boxes.first()).toBeVisible({ timeout: 30_000 });
    for (const box of await boxes.all()) {
      const label = (await box.getAttribute('aria-label')) ?? '';
      const keep = label === `Add ${BBC}` || label === `Add ${TUOITRE}`;
      if ((await box.isChecked()) !== keep) await box.setChecked(keep);
    }
    await wizard.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(wizard.getByTestId('starter-row-bbc_world')).toContainText(/Done|Already added/i, { timeout: 120_000 });
    await wizard.getByRole('button', { name: 'Build my dashboard' }).click();
    // The preset picker opens in place with the Monitor preset preselected.
    const picker = page.getByRole('dialog').filter({ hasText: 'Apply preset' });
    await expect(picker).toBeVisible();
    await expect(picker.getByRole('button', { name: /^Monitor/, pressed: true })).toBeVisible({ timeout: 30_000 });
    await picker.getByRole('button', { name: 'Apply preset' }).click();
    await expect(picker).toBeHidden({ timeout: 60_000 });
    await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
    dashboardUrl = new URL(page.url()).pathname;
    // Applying created a dashboard whose gadgets render (at least one gadget region).
    await expect(page.getByRole('region').first()).toBeVisible({ timeout: 30_000 });
  });

  test('sources page: upload entry and REST "Test connection" tree fills items path', async ({ page }) => {
    await page.goto('/settings/sources');
    await page.getByRole('button', { name: 'Add datasource' }).click();
    await expect(page.getByRole('menuitem', { name: /Upload a file/ })).toBeVisible();
    await page.keyboard.press('Escape');

    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: /^REST API/ }).click();
    await dialog.getByLabel('Source name').fill('e2e-ux rest');
    await dialog.getByRole('button', { name: 'Next', exact: true }).click(); // creates the draft source
    await dialog.getByRole('button', { name: 'Next', exact: true }).click(); // connect -> choose data
    await page.locator('#source-config-url').fill('https://hn.algolia.com/api/v1/search?query=oil&tags=story');
    await dialog.getByRole('button', { name: 'Test connection' }).click();
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
    await page.getByRole('button', { name: 'Edit layout' }).click();
    await page.getByRole('button', { name: 'Add group' }).click();
    const dialog = page.getByRole('dialog', { name: 'Create group' });
    await dialog.getByLabel('Group name').fill(PAGE);
    await dialog.getByRole('radio', { name: 'Three columns' }).check();
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });
    await expect(page.getByRole('tab', { name: PAGE })).toBeVisible({ timeout: 30_000 });
    expect(await pages.count()).toBeGreaterThan(before);

    // From template -> News opens the preset picker in append mode on the active dashboard.
    await page.getByRole('button', { name: 'Add group' }).click();
    await dialog.getByRole('button', { name: 'News', exact: true }).click();
    const picker = page.getByRole('dialog').filter({ hasText: 'Apply preset' });
    await expect(picker.getByRole('button', { name: 'Add to current dashboard', pressed: true })).toBeVisible({ timeout: 30_000 });
    const groupsBefore = await page.getByRole('tab').count();
    await picker.getByRole('button', { name: 'Apply preset' }).click();
    await expect(picker).toBeHidden({ timeout: 60_000 });
    await expect.poll(() => page.getByRole('tab').count(), { timeout: 30_000 }).toBeGreaterThan(groupsBefore);
  });

  test('adds a two-source feed gadget', async ({ page }) => {
    await openDash(page, PAGE);
    await page.getByRole('button', { name: 'Edit layout' }).click();
    await page.getByRole('button', { name: 'Add gadget', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Add gadget to dashboard' });
    await dialog.getByRole('button', { name: /^Feed\b/ }).click();
    for (const label of [BBC, TUOITRE]) {
      await dialog.getByRole('checkbox', { name: new RegExp(`^${escapeRe(label)}`) }).click();
    }
    await dialog.getByLabel('Name', { exact: true }).fill(FEED_NAME);
    const add = dialog.getByRole('button', { name: 'Add gadget', exact: true });
    await expect(add).toBeEnabled({ timeout: 120_000 });
    await add.click();
    await expect(dialog).toBeHidden({ timeout: 60_000 });
    const save = page.getByRole('button', { name: 'Save layout' });
    if (await save.isEnabled()) await save.click();
    await page.getByRole('button', { name: 'Done editing' }).click();
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
    await expect(feed.getByRole('tablist', { name: 'Filter by source' })).toBeVisible({ timeout: 15_000 });
    await expect(feed.getByRole('tab')).toHaveCount(3); // All + 2 sources
    const layout = feed.getByRole('group', { name: 'Feed layout' });
    await layout.getByRole('button', { name: 'Cards' }).click();
    const more = feed.getByRole('button', { name: /^Show \d+ more$/ });
    await expect(more).toBeVisible({ timeout: 15_000 });
    await more.click();
    await expect(feed.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true');
    await openDash(page, PAGE);
    await expect(feedRegion(page).getByRole('group', { name: 'Feed layout' }).getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true', { timeout: 15_000 });
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
    await feed.getByRole('group', { name: 'Feed layout' }).getByRole('button', { name: 'Cards' }).click({ timeout: 15_000 });
    const images = feed.locator('img[src^="/api/v1/media/proxy"]');
    await expect(images.first()).toBeAttached({ timeout: 15_000 });
    await expect.poll(() => images.first().evaluate((img: HTMLImageElement) => img.naturalWidth), { timeout: 15_000 }).toBeGreaterThan(0);
    await setRemoteMedia(page, false);
  });

  test('density and map collapse persist across reload (desktop)', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await openDash(page, 'Overview');
    await page.getByRole('button', { name: /^Density: Comfortable/ }).click();
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
    await page.getByRole('button', { name: /^Density: Compact/ }).click();
    await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact');

    // The map lives on the Overview page (default dashboard group).
    const collapse = page.getByRole('button', { name: 'Collapse map' });
    await expect(collapse).toBeVisible({ timeout: 30_000 });
    await collapse.click();
    await expect(page.getByRole('button', { name: 'Expand map' })).toBeVisible();
    await openDash(page, 'Overview');
    await expect(page.getByRole('button', { name: 'Expand map' })).toBeVisible({ timeout: 30_000 });
    await page.getByRole('button', { name: 'Expand map' }).click();
    await expect(page.getByRole('button', { name: 'Collapse map' })).toBeVisible();
  });
});
