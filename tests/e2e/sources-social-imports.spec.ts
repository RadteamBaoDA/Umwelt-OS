import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { connectWizardSource, deleteSources, escapeRe, findSources, writeHeaders, type WizardCase } from './sources-helpers';

/**
 * Social (Bluesky, Mastodon) and import (Notes, Personal finance, plain manual upload) datasources end to end: each is
 * connected through the real UI, then one Dashboard gets a Table panel gadget per source that must render the records.
 * Needs outbound public network access to public.api.bsky.app and mastodon.social; use the disposable stack only.
 * Everything created is named with PREFIX so cleanup never touches other data. `SOC_ONLY=id1,id2` narrows a debug run.
 */
const PREFIX = 'e2e-soc';
const DASHBOARD = `${PREFIX} datasources`;
const FIXTURES = path.resolve(__dirname, '../fixtures/e2e');
const ROW_SELECTOR = 'tbody tr';
/** Visible copy that means "no data" or "failed"; none may appear in a gadget that claims to render data. */
const NO_DATA = /No provider observations|No current records|Could not load|Feed stream empty|No current stories match/i;

const only = process.env.SOC_ONLY?.split(',');
const selected = (id: string) => !only || only.includes(id);

const SOCIAL: WizardCase[] = [
  { id: 'bluesky', card: 'Bluesky', expect: 'fetch', fields: { 'source-scope-bluesky_actor': 'bsky.app' } },
  { id: 'mastodon', card: 'Mastodon', expect: 'fetch', fields: { 'source-scope-mastodon_instance_url': 'https://mastodon.social', 'source-scope-mastodon_account': 'Mastodon' } },
];
const IDS = ['bluesky', 'mastodon', 'notes', 'finance', 'manual'] as const;
const sourceName = (id: string) => `${PREFIX} ${id}`;

/** Opens the e2e dashboard by id (resolved through the API so tests stay independent of each other). */
async function openDashboard(page: Page) {
  const list = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string }[];
  const dash = list.find((d) => d.name === DASHBOARD);
  expect(dash, 'e2e dashboard exists (run the dashboard creation test first)').toBeTruthy();
  await page.goto(`/dashboard/${dash!.id}`);
  await expectShellUp(page);
  await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
}

/** Fails within 30 s (instead of the full test timeout) when the shell shows its dead "Workspace is unavailable" screen. */
async function expectShellUp(page: Page) {
  const dead = page.getByRole('heading', { name: 'Workspace is unavailable' });
  const up = page.getByRole('button', { name: 'Switch dashboard' }).or(page.getByRole('button', { name: 'Edit layout' }));
  await expect(up.or(dead).first()).toBeVisible({ timeout: 30_000 });
  expect(await dead.isVisible(), 'workspace shell is unavailable').toBe(false);
}

const regionOf = (page: Page, id: string) => page.getByRole('region', { name: sourceName(id), exact: true });

/** Adds a Custom/Table panel gadget bound to the source named `sourceName(id)` through Edit layout -> Add gadget. */
async function addGadget(page: Page, id: string) {
  await openDashboard(page);
  await page.getByRole('button', { name: 'Edit layout' }).click();
  await page.getByRole('button', { name: 'Add gadget', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add gadget to dashboard' });
  await dialog.getByRole('button', { name: /^Custom/ }).click();
  await dialog.getByRole('combobox', { name: 'Display type' }).click();
  await page.getByRole('option', { name: 'Table panel', exact: true }).click();
  const name = new RegExp(`^${escapeRe(sourceName(id))}`);
  await dialog.getByRole('radio', { name }).or(dialog.getByRole('checkbox', { name })).first().click();
  await dialog.getByLabel('Name', { exact: true }).fill(sourceName(id));
  const add = dialog.getByRole('button', { name: 'Add gadget', exact: true });
  await expect(add).toBeEnabled({ timeout: 60_000 });
  await add.click();
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  const save = page.getByRole('button', { name: 'Save layout' });
  if (await save.isEnabled()) await save.click();
  await page.getByRole('button', { name: 'Done editing' }).click();
}

/** Asserts the gadget shows at least one data row, no alert and none of the empty/error copy (reloading to await async ingestion). */
async function expectRendersData(page: Page, id: string, attempts = 6) {
  let last: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const region = regionOf(page, id);
    try {
      await expect(region.locator(ROW_SELECTOR).first(), 'a data row is rendered').toBeVisible({ timeout: 15_000 });
      await expect(region.getByRole('alert'), 'no error state').toHaveCount(0);
      await expect(region).not.toContainText(NO_DATA);
      console.log(`RESULT gadget ${id} :: ${await region.locator(ROW_SELECTOR).count()} rows rendered`);
      return;
    } catch (error) {
      last = error;
      await page.reload();
      await expectShellUp(page);
    }
  }
  throw last;
}

/** Waits for the upload progress panel to report chunked records, i.e. the ingestion run produced content. */
async function expectUploadIngested(page: Page) {
  await expect(page.getByText(/Chunked: [1-9]/)).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(/Upload failed|Error:/i)).toHaveCount(0);
  console.log(`RESULT upload :: ${(await page.getByText(/Chunked:/).innerText()).trim()}`);
}

/** Creates a manual-import source (Notes or Personal finance) through its settings button and uploads a fixture. */
async function importViaWizard(page: Page, button: string, id: string, file: string) {
  await page.goto('/settings/sources');
  await page.getByRole('button', { name: button, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Source name').fill(sourceName(id));
  await dialog.getByRole('button', { name: 'Next', exact: true }).click();
  await dialog.locator('#upload-file').setInputFiles(path.join(FIXTURES, file));
  await dialog.getByRole('button', { name: 'Upload', exact: true }).click();
  await expectUploadIngested(page);
  await dialog.getByRole('button', { name: 'Done', exact: true }).click();
}

test.describe('social and import datasources', () => {
  test('clean slate', async ({ page }) => {
    await deleteSources(page, PREFIX);
    expect(await findSources(page, PREFIX)).toEqual([]);
  });

  for (const c of SOCIAL.filter((x) => selected(x.id))) {
    test(`source ${c.id}`, async ({ page }) => {
      test.setTimeout(180_000);
      const out = await connectWizardSource(page, c, sourceName(c.id));
      console.log(`RESULT ${c.id} :: ${out.text}`);
      expect(out.ok, out.text).toBe(true);
      expect((await findSources(page, sourceName(c.id))).length, 'source created').toBe(1);
    });
  }

  test('source notes (Markdown import)', async ({ page }) => {
    test.skip(!selected('notes'));
    test.setTimeout(180_000);
    await importViaWizard(page, 'Import Notes', 'notes', 'soc-notes.md');
    expect((await findSources(page, sourceName('notes'))).length, 'source created').toBe(1);
  });

  test('source personal finance (CSV import)', async ({ page }) => {
    test.skip(!selected('finance'));
    test.setTimeout(180_000);
    await importViaWizard(page, 'Import Personal Finance', 'finance', 'soc-finance.csv');
    expect((await findSources(page, sourceName('finance'))).length, 'source created').toBe(1);
  });

  test('source manual file upload', async ({ page }) => {
    test.skip(!selected('manual'));
    test.setTimeout(180_000);
    await page.goto('/settings/sources');
    await page.getByRole('button', { name: 'New manual source', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Source name').fill(sourceName('manual'));
    await dialog.getByRole('button', { name: 'Save source', exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });
    await page.goto('/knowledge/documents');
    await page.getByRole('button', { name: 'Upload file', exact: true }).click();
    await page.locator('#upload-source').click();
    await page.getByRole('option', { name: sourceName('manual'), exact: true }).click();
    await page.locator('#upload-file').setInputFiles(path.join(FIXTURES, 'soc-document.md'));
    await page.getByRole('button', { name: 'Upload', exact: true }).click();
    await expectUploadIngested(page);
  });
});

test.describe('dashboard', () => {
  test('create the dashboard', async ({ page }) => {
    await page.goto('/dashboard');
    await page.getByRole('button', { name: 'Switch dashboard' }).click();
    await page.getByRole('menuitem', { name: 'New dashboard' }).click();
    await page.getByLabel('Dashboard name').fill(DASHBOARD);
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(page).toHaveURL(/\/dashboard\/[0-9a-f-]{36}/);
    await expect(page.getByRole('button', { name: 'Switch dashboard' })).toContainText(DASHBOARD);
  });

  for (const id of IDS.filter(selected)) {
    test(`gadget ${id}: Table panel renders data`, async ({ page }) => {
      test.setTimeout(180_000);
      test.skip((await findSources(page, sourceName(id))).length === 0, `source ${id} was not created (see its source test)`);
      await addGadget(page, id);
      await expectRendersData(page, id);
    });
  }

  test('dashboard has no gadget error states and no console errors', async ({ page }) => {
    test.setTimeout(180_000);
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(`pageerror ${e.message}`));
    page.on('response', (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
    await openDashboard(page);
    const missing: string[] = [];
    for (const id of IDS.filter(selected)) {
      if ((await findSources(page, sourceName(id))).length === 0) continue;
      if (!(await regionOf(page, id).waitFor({ timeout: 5_000 }).then(() => true, () => false))) missing.push(id);
    }
    expect(missing, 'planned gadgets missing from the dashboard').toEqual([]);
    await page.waitForLoadState('networkidle').catch(() => undefined);
    await page.waitForTimeout(5_000);
    const failing = await page.getByRole('region', { name: new RegExp(`^${PREFIX} `) }).filter({ has: page.getByRole('alert') })
      .evaluateAll((nodes) => nodes.map((node) => node.querySelector('h3')?.textContent ?? '?'));
    expect(failing, 'gadgets showing an error state').toEqual([]);
    expect(errors, 'console errors on the dashboard').toEqual([]);
  });

  test('cleanup', async ({ page }) => {
    await page.goto('/dashboard');
    const headers = await writeHeaders(page);
    const dashboards = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string; revision: number }[];
    for (const d of dashboards.filter((x) => x.name === DASHBOARD)) {
      expect((await page.request.delete(`/api/v1/dashboards/${d.id}?expected_revision=${d.revision}`, { headers })).status(), 'delete dashboard').toBeLessThan(300);
    }
    const defs = await (await page.request.get('/api/v1/gadget-definitions')).json() as { id: string; name: string; revision: number }[];
    for (const d of defs.filter((x) => x.name.startsWith(PREFIX))) {
      expect((await page.request.delete(`/api/v1/gadget-definitions/${d.id}?expected_revision=${d.revision}`, { headers })).status(), `delete definition ${d.name}`).toBeLessThan(300);
    }
    await deleteSources(page, PREFIX);
  });
});
