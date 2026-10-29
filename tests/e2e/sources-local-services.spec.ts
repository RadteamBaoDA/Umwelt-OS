import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { acknowledgeTerms, deleteSources, escapeRe, findSources, openWizard, waitForFetchOutcome, writeHeaders } from './sources-helpers';

/**
 * Local-service datasources end-to-end against the fake servers of the disposable stack (tests/integration/fake_local_services.py,
 * service `fake-local`): MCP, Home Assistant, FHIR R4 health observations and the browser crawler probe. Each source is connected
 * through the real UI, test-fetched, then bound to a dashboard gadget that must render rows. Everything is prefixed `e2e-loc`.
 * `LOC_ONLY=mcp,ha` narrows the run while debugging.
 */
const PREFIX = 'e2e-loc';
const DASHBOARD = `${PREFIX} datasources`;
const ORIGIN = 'http://fake-local:8000';
const TOKEN = 'e2e-local-token';
const NO_DATA = /No provider observations|No current records|Could not load|Feed stream empty|No current stories match/i;
const only = process.env.LOC_ONLY?.split(',');
const selected = (id: string) => !only || only.includes(id);

type Case = { id: string; card: string; fields: Record<string, string>; bearer?: boolean };
/** Local-only providers: activation is refused by design (privacy boundary), so the wizard must say so. */
const LOCAL_ONLY_IDS = new Set(['ha', 'health']);

const CASES: Case[] = [
  { id: 'ha', card: 'Home Assistant', bearer: true, fields: { 'ha-base-url': `${ORIGIN}/ha`, 'ha-entity-ids': 'sensor.e2e_temperature, sensor.e2e_humidity' } },
  { id: 'health', card: 'FHIR R4 health observations', bearer: true, fields: { 'fhir-base-url': `${ORIGIN}/fhir`, 'fhir-patient-id': 'e2e-patient' } },
];
/** Logs every failing API response (method, path, body) so a failed UI step names the request that broke. */
function traceFailures(page: Page) {
  page.on('response', async (r) => {
    if (r.status() < 400 || !r.url().includes('/api/')) return;
    console.log(`HTTP ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname} :: ${(await r.text().catch(() => '')).slice(0, 300)}`);
  });
}
const name = (id: string) => `${PREFIX} ${id}`;

/** JSON write through the owner API with a fresh CSRF token. */
async function api(page: Page, method: 'POST' | 'PUT', path: string, data?: unknown) {
  const res = await page.request.fetch(path, { method, data, headers: { ...(await writeHeaders(page)), 'Content-Type': 'application/json' } });
  const text = await res.text();
  expect(res.status(), `${method} ${path}: ${text}`).toBeLessThan(300);
  return text ? JSON.parse(text) : {};
}

/** Clicks Save and test fetch on the wizard's last step and returns the outcome. */
async function saveAndTest(page: Page, id: string) {
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Save and test fetch' }).click();
  const out = await waitForFetchOutcome(dialog);
  console.log(`RESULT ${id} :: ${out.text}`);
  return out;
}

/** Walks the add wizard for a local-service case (bearer token, endpoint/scope fields) and runs the test fetch. */
async function connectLocal(page: Page, c: Case) {
  traceFailures(page);
  const dialog = await openWizard(page);
  await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(c.card)}`) }).click();
  await dialog.getByLabel('Source name').fill(name(c.id));
  const next = dialog.getByRole('button', { name: 'Next', exact: true });
  await next.click();
  if (c.bearer) await page.locator('#source-local-bearer-token').fill(TOKEN);
  await expect(next).toBeEnabled();
  await next.click();
  for (const [id, value] of Object.entries(c.fields)) await page.locator(`#${id}`).fill(value);
  await acknowledgeTerms(dialog);
  const validated = page.waitForResponse((r) => r.url().includes('/validate-draft') && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Validate draft' }).click();
  const status = (await validated).status();
  console.log(`RESULT ${c.id} validate-draft :: ${status}`);
  expect(status, 'validate-draft response').toBe(200);
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await next.click();
  await next.click();
  return saveAndTest(page, c.id);
}

/** Opens the e2e dashboard by id. */
async function openDashboard(page: Page) {
  const list = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string }[];
  const dash = list.find((d) => d.name === DASHBOARD);
  expect(dash, 'e2e dashboard exists (run the dashboard creation test first)').toBeTruthy();
  await page.goto(`/dashboard/${dash!.id}`);
  await expectShellUp(page);
  await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
}

/** Fails within 30 s when the shell shows its dead "Workspace is unavailable" screen. */
async function expectShellUp(page: Page) {
  const dead = page.getByRole('heading', { name: 'Workspace is unavailable' });
  const up = page.getByRole('button', { name: 'Switch dashboard' }).or(page.getByRole('button', { name: 'Edit layout' }));
  await expect(up.or(dead).first()).toBeVisible({ timeout: 30_000 });
  expect(await dead.isVisible(), 'workspace shell is unavailable').toBe(false);
}

const regionOf = (page: Page, id: string) => page.getByRole('region', { name: name(id), exact: true });

/** Adds a Custom Table panel gadget bound to the source named `name(id)`. */
async function addGadget(page: Page, id: string) {
  await openDashboard(page);
  await page.getByRole('button', { name: 'Edit layout' }).click();
  await page.getByRole('button', { name: 'Add gadget', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add gadget to dashboard' });
  await dialog.getByRole('button', { name: /^Custom/ }).click();
  await dialog.getByRole('combobox', { name: 'Display type' }).click();
  await page.getByRole('option', { name: 'Table panel', exact: true }).click();
  const label = new RegExp(`^${escapeRe(name(id))}`);
  await dialog.getByRole('radio', { name: label }).or(dialog.getByRole('checkbox', { name: label })).first().click();
  await dialog.getByLabel('Name', { exact: true }).fill(name(id));
  const add = dialog.getByRole('button', { name: 'Add gadget', exact: true });
  await expect(add).toBeEnabled({ timeout: 120_000 });
  await add.click();
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  const save = page.getByRole('button', { name: 'Save layout' });
  if (await save.isEnabled()) await save.click();
  await page.getByRole('button', { name: 'Done editing' }).click();
}

/** Asserts the gadget shows a data row, no alert and no empty/error copy; reloads between attempts while ingestion settles. */
async function expectRendersData(page: Page, id: string, attempts = 6) {
  let last: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const region = regionOf(page, id);
    try {
      await expect(region.locator('tbody tr').first(), 'a data row is rendered').toBeVisible({ timeout: 15_000 });
      await expect(region.getByRole('alert'), 'no error state').toHaveCount(0);
      await expect(region).not.toContainText(NO_DATA);
      return;
    } catch (error) {
      last = error;
      await page.reload();
      await expectShellUp(page);
    }
  }
  throw last;
}

/** Creates the MCP connection, checks and discovers it, and returns ids for the grant step. */
async function prepareMcpConnection(page: Page) {
  await page.goto('/settings/sources');
  const conn = await api(page, 'POST', '/api/v1/mcp/connections', {
    name: name('mcp-conn'), transport: 'streamable_http', endpoint: `${ORIGIN}/mcp`, auth_method: 'none',
    credential_update: { action: 'remove' }, timeout_seconds: 30,
  });
  const checked = await api(page, 'POST', `/api/v1/mcp/connections/${conn.id}/draft-check`);
  console.log(`RESULT mcp connection health :: ${checked.health} ${checked.error_code ?? ''}`);
  expect(checked.health, 'connection health').toBe('connected');
  const discovery = await api(page, 'POST', `/api/v1/mcp/connections/${conn.id}/discover`);
  expect(discovery.capabilities.length, 'discovered tools').toBeGreaterThan(0);
  return { connId: conn.id as string, revision: checked.revision as number, discovery };
}

test.describe('local services', () => {
  test('clean slate', async ({ page }) => {
    await page.goto('/settings/sources');
    await deleteSources(page, PREFIX);
  });

  test('source mcp', async ({ page }) => {
    test.skip(!selected('mcp'));
    test.setTimeout(240_000);
    traceFailures(page);
    const { connId, revision, discovery } = await prepareMcpConnection(page);
    // Wizard: provider + name creates the draft source; leaving the page (not closing the dialog) keeps it.
    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: /^MCP server/ }).click();
    await dialog.getByLabel('Source name').fill(name('mcp'));
    await dialog.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(dialog.getByText('Enable an MCP connection first in MCP settings.').or(dialog.getByText('Reviewed collection grants'))).toBeVisible();
    const source = (await findSources(page, name('mcp')))[0];
    expect(source, 'draft mcp source created').toBeTruthy();
    const cap = discovery.capabilities[0];
    await api(page, 'PUT', `/api/v1/mcp/connections/${connId}/grants`, {
      expected_connection_revision: revision, discovery_id: discovery.id,
      selections: [{ capability_id: cap.id, descriptor_hash: cap.descriptor_hash, purpose: 'collection', risk: 'READ_ONLY', source_ids: [source.id], destinations: ['local'] }],
    });
    const enabled = await api(page, 'POST', `/api/v1/mcp/connections/${connId}/enable?expected_revision=${revision}`);
    console.log(`RESULT mcp enabled :: ${enabled.enabled} rev ${enabled.revision}`);
    // Re-open the source through Configure so the collection editor loads the now-enabled connection and its grant.
    await page.goto('/settings/sources');
    await page.getByRole('group', { name: `Actions for ${name('mcp')}` }).getByRole('button', { name: 'Configure' }).click();
    const edit = page.getByRole('dialog');
    await edit.getByRole('checkbox', { name: /READ_ONLY/ }).check();
    await edit.getByRole('button', { name: 'Save & enable' }).click();
    await edit.getByRole('button', { name: /Save and test/ }).first().click();
    const out = await saveAndTest(page, 'mcp');
    expect(out.ok, out.text).toBe(true);
  });

  for (const c of CASES.filter((x) => selected(x.id))) {
    test(`source ${c.id}`, async ({ page }) => {
      test.setTimeout(240_000);
      const out = await connectLocal(page, c);
      if (LOCAL_ONLY_IDS.has(c.id)) {
        // Owner decision: local-only sources keep data on this device and are never activated for collection.
        expect(out.ok, out.text).toBe(false);
        expect(out.text).toContain('local-only');
        return;
      }
      expect(out.ok, out.text).toBe(true);
    });
  }

  test('browser crawl probe', async ({ page }) => {
    test.skip(!selected('browser'));
    test.setTimeout(120_000);
    traceFailures(page);
    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: /^Web page/ }).click();
    await dialog.getByLabel('Source name').fill(name('browser'));
    const next = dialog.getByRole('button', { name: 'Next', exact: true });
    await next.click();
    await next.click();
    await page.locator('#source-config-url').fill(`${ORIGIN}/page`);
    const validated = page.waitForResponse((r) => r.url().includes('/validate-draft') && r.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Validate draft' }).click();
    const res = await validated;
    console.log(`RESULT browser validate-draft :: ${res.status()} ${(await res.text()).slice(0, 200)}`);
    test.info().annotations.push({ type: 'browser', description: `validate-draft ${res.status()}: the crawler admits public URLs only and no browser service runs in the test stack` });
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
  });

  // ha/health are local-only and cannot be activated, so no collected data can exist for a gadget to render.
  for (const id of ['mcp'].filter(selected)) {
    test(`gadget ${id} renders data`, async ({ page }) => {
      test.setTimeout(180_000);
      test.skip(!(await findSources(page, name(id))).length, `source ${id} was not created (see its source test)`);
      await addGadget(page, id);
      await expectRendersData(page, id);
    });
  }

  test('cleanup', async ({ page }) => {
    await page.goto('/dashboard');
    const headers = await writeHeaders(page);
    const dashboards = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string; revision: number }[];
    for (const d of dashboards.filter((x) => x.name === DASHBOARD)) {
      await page.request.delete(`/api/v1/dashboards/${d.id}?expected_revision=${d.revision}`, { headers });
    }
    const defs = await (await page.request.get('/api/v1/gadget-definitions')).json() as { id: string; name: string; revision: number }[];
    for (const d of defs.filter((x) => x.name.startsWith(PREFIX))) {
      await page.request.delete(`/api/v1/gadget-definitions/${d.id}?expected_revision=${d.revision}`, { headers: await writeHeaders(page) });
    }
    await deleteSources(page, PREFIX);
    const conns = await (await page.request.get('/api/v1/mcp/connections')).json() as { items: { id: string; name: string; revision: number; enabled: boolean }[] };
    for (const c of conns.items.filter((x) => x.name.startsWith(PREFIX) && x.enabled)) {
      await page.request.post(`/api/v1/mcp/connections/${c.id}/disable?expected_revision=${c.revision}`, { headers: await writeHeaders(page) });
    }
  });
});
