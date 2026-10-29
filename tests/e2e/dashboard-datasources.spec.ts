import type { Locator, Page, Response } from '@playwright/test';
import { test, expect } from './fixtures';
import {
  BUILTINS, WIZARD_CASES, connectWizardSource, deleteSources, enableBuiltinViaSettings, escapeRe, findSources, writeHeaders,
} from './sources-helpers';

/**
 * Datasource -> dashboard gadget end-to-end: connects every datasource that can fetch without an owner-supplied key
 * (built-ins, REST, RSS, web page and the free scoped providers), then builds one Dashboard through the real UI
 * (New dashboard, Edit layout, Add gadget editor, Save layout) with a gadget bound to each datasource and asserts that
 * each gadget renders real data, and that the finished dashboard produces no console errors. Keyed providers are only
 * connected (dummy key) and must fail cleanly. Needs outbound public network access; use the disposable stack only.
 *
 * Tests run in file order in one worker: sources first, then the dashboard, one gadget per test, a final render check
 * and cleanup. Everything created is named with PREFIX so cleanup never touches other data.
 */
const PREFIX = 'e2e-dash';
const DASHBOARD = `${PREFIX} datasources`;
/**
 * Remote-throttled providers: GDELT answers 429 to an IP for minutes after a burst (checked 2026-10-26). Waiting the full
 * ~5 min retry ladder made one throttled run cost 8 min, so a source still marked `provider_rate_limited` after this wait
 * counts as a remote throttle the app handled (annotated, gadget skipped) instead of a failure.
 */
const SLOW_MS: Record<string, number> = { gdelt_economy: 180_000 };
/** Endpoints refused from this network (cisa.gov answers 403); covered by sources-builtin-fetch.spec.ts. */
const EXTERNAL_BLOCK = new Set(['cisa_kev']);
/**
 * Feeds that are legitimately empty right now: the NOAA tsunami Atom feed carries no entries unless a bulletin is
 * active (checked 2026-10-24); arXiv's daily RSS is empty on weekends (checked Sat 2026-10-24), which the app reports as `collection_incomplete`. No data exists to bind a gadget to.
 */
const EMPTY_FEED = new Set(['noaa_tsunami', 'arxiv']);

/** Where a gadget's data shows up once rendered, per renderer id. */
const ROW_SELECTOR: Record<string, string> = {
  news_feed: 'li',
  table_panel: 'tbody tr',
  provider_observation: 'li',
  weather: 'li, tbody tr, dl',
  finance_chart: 'svg',
};
/** Visible copy that means "no data" or "failed"; none may appear in a gadget that claims to render data. */
const NO_DATA = /No provider observations|No current records|Could not load|Feed stream empty|No current stories match/i;

type Gadget = {
  /** Source key: built-in id or wizard case id. */
  id: string;
  /** Template tile label (en-us), or `null` for the Custom tile with `renderer`. */
  template: string | null;
  /** Renderer id; for the Custom tile also looked up by its en-us label in `RENDERER_LABEL`. */
  renderer: keyof typeof ROW_SELECTOR;
  /** Enable the built-in from inside the gadget editor instead of Settings (exercises that UI path). */
  viaPicker?: boolean;
  /** Advanced-section fields to fill (label -> value). */
  advanced?: Record<string, string>;
  /** Evidenced product limitation: the gadget is expected to fail (Playwright reports an unexpected pass). */
  knownLimit?: string;
};

const RENDERER_LABEL: Record<string, string> = {
  news_feed: 'News feed', table_panel: 'Table panel', provider_observation: 'Provider observation', weather: 'Weather', finance_chart: 'Finance chart',
};
const tpl = (id: string, template: string, renderer: Gadget['renderer'], extra: Partial<Gadget> = {}): Gadget => ({ id, template, renderer, ...extra });
const custom = (id: string, renderer: Gadget['renderer'], extra: Partial<Gadget> = {}): Gadget => ({ id, template: null, renderer, ...extra });

/** One gadget per datasource; the renderer is the one the gadget editor offers for that provider's data shape. */
const GADGETS: Gadget[] = [
  // News lists from the templates.
  ...['bbc_world', 'aljazeera_world', 'npr_news'].map((id) => tpl(id, 'World news', 'news_feed')),
  ...['vnexpress_business', 'tuoitre_news', 'thanhnien_news', 'dantri_news'].map((id) => tpl(id, 'Vietnam business news', 'news_feed')),
  ...['hn_top', 'lobsters'].map((id) => tpl(id, 'Tech news', 'news_feed')),
  // Price / metric observations from the templates (alternative_me is enabled from inside the editor).
  tpl('alternative_me', 'Fear & Greed index', 'provider_observation', { viaPicker: true }),
  ...['binance', 'coinpaprika'].map((id) => tpl(id, 'Bitcoin price', 'provider_observation')),
  tpl('coingecko', 'Bitcoin price', 'provider_observation'),
  ...['frankfurter', 'ecb', 'open_er_api'].map((id) => tpl(id, 'Exchange rates', 'provider_observation')),
  tpl('usgs', 'Earthquakes', 'provider_observation'),
  tpl('open_meteo', 'Weather', 'weather'),
  custom('world_bank', 'provider_observation', {
    advanced: { 'Date range (days)': '366' },
    knownLimit: 'Annual GDP is observed on Jan 1 of its year (2025-01-01), older than the 366-day maximum observation window.',
  }),
  custom('alpha_vantage', 'finance_chart', { advanced: { 'Tracked symbols (comma-separated)': 'IBM' } }),
  // Generic news lists for feeds without a template.
  ...['rss', 'google_news', 'wikipedia_mostread', 'wikipedia_top_views', 'fed_press', 'gdacs_rss', 'us_travel_advisories', 'noaa_tsunami', 'jma_eqvol', 'gdelt_economy']
    .map((id) => custom(id, 'news_feed')),
  // Record tables for event / document / measurement sources.
  ...['rest', 'web', 'nasa_eonet', 'sec_edgar', 'openalex', 'arxiv', 'huggingface', 'github_releases', 'youtube', 'gdacs_events', 'nws_alerts',
    'emsc_quakes', 'launch_library', 'bmkg_quakes', 'satnogs_observations', 'noaa_kp', 'oecd_cli', 'iss_position', 'disease_sh', 'ioda', 'nvd']
    .map((id) => custom(id, 'table_panel')),
];

const wizardCase = (id: string) => WIZARD_CASES.find((c) => c.id === id);
const builtinLabel = (id: string) => BUILTINS.find(([key]) => key === id)?.[1];
/** Optional `E2E_ONLY=id1,id2` narrows the plan while debugging a single datasource. */
const only = process.env.E2E_ONLY?.split(',');
const selected = (id: string) => !only || only.includes(id);

/** Source display name the picker shows for a plan entry. */
function pickerName(id: string): string {
  return wizardCase(id) ? `${PREFIX} ${id}` : builtinLabel(id)!;
}

/** Looks up the source id for a plan entry (wizard sources by name, built-ins by the catalog). */
async function sourceIdOf(page: Page, id: string): Promise<string | null> {
  if (wizardCase(id)) return (await findSources(page, `${PREFIX} ${id}`))[0]?.id ?? null;
  const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; source_id: string | null }[];
  return builtins.find((b) => b.id === id)?.source_id ?? null;
}

/** True when a slow provider's built-in source is parked on a retryable remote 429 (see SLOW_MS). */
async function remoteThrottled(page: Page, id: string): Promise<boolean> {
  const label = builtinLabel(id);
  if (!SLOW_MS[id] || !label) return false;
  const source = (await findSources(page, label))[0];
  return [source?.collection_error_code, source?.last_error_code].includes('provider_rate_limited');
}

/** Opens the e2e dashboard by id (resolved through the API so tests stay independent of each other). */
async function openDashboard(page: Page) {
  const list = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string }[];
  const dash = list.find((d) => d.name === DASHBOARD);
  expect(dash, 'e2e dashboard exists (run the dashboard creation test first)').toBeTruthy();
  await page.goto(`/dashboard/${dash!.id}`);
  await expectShellUp(page);
  await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
  // The route overlay stays up until gadgets settle (capped after dashboard readiness); clicks under it would time out.
  await expect(page.locator('[data-route-loader]')).toBeHidden({ timeout: 30_000 });
}

/** Fails within 30 s (instead of the full test timeout) when the shell shows its dead "Workspace is unavailable" screen. */
async function expectShellUp(page: Page) {
  const dead = page.getByRole('heading', { name: 'Workspace is unavailable' });
  const up = page.getByRole('button', { name: 'Switch dashboard' }).or(page.getByRole('button', { name: 'Edit layout' }));
  await expect(up.or(dead).first()).toBeVisible({ timeout: 30_000 });
  expect(await dead.isVisible(), 'workspace shell is unavailable').toBe(false);
}

/** Gadget region for a plan entry, found by the unique title the editor set. */
const regionOf = (page: Page, id: string) => page.getByRole('region', { name: `${PREFIX} ${id}`, exact: true });

/** Adds one gadget through Edit layout -> Add gadget -> editor -> Save layout. */
async function addGadget(page: Page, g: Gadget) {
  await openDashboard(page);
  await page.getByRole('button', { name: 'Edit layout' }).click();
  await page.getByRole('button', { name: 'Add gadget', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add gadget to dashboard' });
  if (g.template) {
    await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(g.template)}`) }).click();
  } else {
    await dialog.getByRole('button', { name: /^Custom/ }).click();
    await dialog.getByRole('combobox', { name: 'Display type' }).click();
    await page.getByRole('option', { name: RENDERER_LABEL[g.renderer], exact: true }).click();
  }
  const name = new RegExp(`^${escapeRe(pickerName(g.id))}`);
  if (g.viaPicker) {
    await dialog.getByRole('radio', { name }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Enable and use' }).click();
    await expect(page.getByRole('alertdialog')).toBeHidden({ timeout: 60_000 });
  } else {
    await dialog.getByRole('radio', { name }).or(dialog.getByRole('checkbox', { name })).first().click();
  }
  await dialog.getByLabel('Name', { exact: true }).fill(`${PREFIX} ${g.id}`);
  if (g.advanced) {
    await dialog.getByText('Advanced', { exact: true }).click();
    for (const [label, value] of Object.entries(g.advanced)) await dialog.getByLabel(label).fill(value);
  }
  // Save stays disabled while a just-enabled source is still activating.
  const add = dialog.getByRole('button', { name: 'Add gadget', exact: true });
  await expect(add).toBeEnabled({ timeout: 120_000 });
  // Record failing gadget/dashboard API responses so a rejected add reports the server's reason, not a bare timeout.
  const failures: string[] = [];
  const onResponse = (r: Response) => {
    const url = new URL(r.url());
    if (r.status() >= 400 && /\/api\/v1\/(gadget-definitions|dashboards)/.test(url.pathname)) {
      failures.push(`${r.request().method()} ${r.status()} ${url.pathname}`);
    }
  };
  page.on('response', onResponse);
  try {
    await add.click();
    const alert = dialog.getByRole('alert');
    // First of: dialog closes (success) or an alert appears in it (rejection); same 60s budget.
    const outcome = await Promise.race([
      dialog.waitFor({ state: 'hidden', timeout: 60_000 }).then(() => 'hidden' as const, () => 'timeout' as const),
      alert.first().waitFor({ state: 'visible', timeout: 60_000 }).then(() => 'alert' as const, () => 'timeout' as const),
    ]);
    if (outcome !== 'hidden') {
      const text = (await alert.first().textContent({ timeout: 1_000 }).catch(() => null))?.trim();
      throw new Error(`Add gadget ${g.id} failed: alert=${text ?? 'none'}; failing responses=[${failures.join('; ')}]`);
    }
  } finally {
    page.off('response', onResponse);
  }
  // Adding creates the instance and its placement server-side; only a leftover draft edit needs an explicit save.
  const save = page.getByRole('button', { name: 'Save layout' });
  if (await save.isEnabled()) await save.click();
  await page.getByRole('button', { name: 'Done editing' }).click();
}

/**
 * Asserts a gadget shows at least one data row, no alert and none of the empty/error copy. Collection and
 * normalization finish asynchronously after a source is enabled, so a miss reloads the dashboard and looks again.
 */
async function expectRendersData(page: Page, id: string, renderer: Gadget['renderer'], attempts = 6) {
  let last: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const region = regionOf(page, id);
    try {
      await expect(region.locator(ROW_SELECTOR[renderer]).first(), 'a data row is rendered').toBeVisible({ timeout: 15_000 });
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

test.describe('datasources', () => {
  test('clean slate', async ({ page }) => {
    await deleteSources(page, PREFIX);
    const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; enabled: boolean }[];
    const planned = new Set(GADGETS.map((g) => g.id));
    const dirty = builtins.filter((b) => b.enabled && planned.has(b.id)).map((b) => b.id);
    expect(dirty, 'built-ins already enabled: use a fresh disposable stack').toEqual([]);
  });

  for (const [id, label] of BUILTINS.filter(([key]) => selected(key) && GADGETS.some((g) => g.id === key && !g.viaPicker))) {
    test(`source built-in ${id}`, async ({ page }) => {
      test.setTimeout(120_000 + (SLOW_MS[id] ?? 0));
      const out = await enableBuiltinViaSettings(page, id, label, SLOW_MS[id] ?? 120_000).catch(async (error: unknown) => {
        if (!(await remoteThrottled(page, id))) throw error;
        return { ok: false, text: 'remote throttle: provider_rate_limited', sourceId: await sourceIdOf(page, id) };
      });
      if (out.text.startsWith('remote throttle')) test.info().annotations.push({ type: 'remote-throttle', description: out.text });
      console.log(`RESULT ${id} :: ${out.text}`);
      expect(out.sourceId, 'built-in source created').not.toBeNull();
      if (EMPTY_FEED.has(id)) {
        test.info().annotations.push({ type: 'empty-feed', description: out.text });
        expect(out.text).toMatch(/Test fetch (succeeded|failed: collection_incomplete)/);
        return;
      }
      if (out.text.startsWith('remote throttle')) return;
      expect(out.ok, out.text).toBe(true);
    });
  }

  for (const c of WIZARD_CASES.filter((x) => selected(x.id) && (GADGETS.some((g) => g.id === x.id) || x.expect === 'rejects'))) {
    test(`source wizard ${c.id}`, async ({ page }) => {
      test.setTimeout(120_000);
      const out = await connectWizardSource(page, c, `${PREFIX} ${c.id}`);
      console.log(`RESULT ${c.id} :: ${out.text}`);
      if (c.expect === 'fetch') expect(out.ok, out.text).toBe(true);
      else {
        expect(out.ok, 'dummy credential must not succeed').toBe(false);
        expect(out.text).toMatch(/Test fetch failed: (invalid_credential|provider_rejected|provider_response_invalid|provider_unavailable)\./);
      }
    });
  }
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

  for (const g of GADGETS.filter((x) => selected(x.id))) {
    test(`gadget ${g.id}: ${g.template ?? 'Custom'} renders data`, async ({ page }) => {
      test.setTimeout(120_000);
      test.skip(EXTERNAL_BLOCK.has(g.id), 'endpoint blocked from this network');
      test.skip(EMPTY_FEED.has(g.id), 'feed currently carries no entries');
      if (g.knownLimit) test.fail(true, g.knownLimit);
      if (!g.viaPicker) test.skip(!(await sourceIdOf(page, g.id)), `source ${g.id} was not created (see its source test)`);
      test.skip(await remoteThrottled(page, g.id), `${g.id} is throttled by the remote provider`);
      await addGadget(page, g);
      await expectRendersData(page, g.id, g.renderer);
    });
  }

  test('dashboard has no gadget error states and no console errors', async ({ page }) => {
    test.setTimeout(300_000);
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(`pageerror ${e.message}`));
    // Console "Failed to load resource" lines carry no URL; record failing API responses so a failure names the request.
    page.on('response', (r) => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
    await openDashboard(page);
    // Every planned gadget whose test was not skipped must be on the dashboard; a missing region is a failure.
    const missing: string[] = [];
    for (const g of GADGETS.filter((x) => selected(x.id) && !EXTERNAL_BLOCK.has(x.id) && !EMPTY_FEED.has(x.id) && !x.knownLimit)) {
      if (await remoteThrottled(page, g.id)) continue;
      if (!(await regionOf(page, g.id).waitFor({ timeout: 5_000 }).then(() => true, () => false))) missing.push(g.id);
    }
    expect(missing, 'planned gadgets missing from the dashboard').toEqual([]);
    // Let every gadget finish loading before looking for error states.
    await page.waitForLoadState('networkidle').catch(() => undefined);
    await page.waitForTimeout(10_000);
    const failing = await page.getByRole('region', { name: new RegExp(`^${PREFIX} `) }).filter({ has: page.getByRole('alert') })
      .evaluateAll((nodes) => nodes.map((node) => node.querySelector('h3')?.textContent ?? '?'));
    // knownLimit gadgets are expected to show an error state (their own test asserts the failure).
    const knownTitles = GADGETS.filter((x) => x.knownLimit).map((x) => `${PREFIX} ${x.id}`);
    expect(failing.filter((t) => !knownTitles.includes(t)), 'gadgets showing an error state').toEqual([]);
    expect(errors, 'console errors on the dashboard').toEqual([]);
  });

  test('cleanup', async ({ page }) => {
    // Owner writes need a same-origin Origin header, which writeHeaders derives from the current page URL.
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
    // Built-ins keep their catalog names, so they are removed by id. Headers are re-read per delete because the CSRF token rotates.
    for (const g of GADGETS) {
      if (wizardCase(g.id)) continue;
      const id = await sourceIdOf(page, g.id);
      if (id) expect((await page.request.delete(`/api/v1/sources/${id}`, { headers: await writeHeaders(page) })).status(), `delete source ${g.id}`).toBeLessThan(300);
    }
  });
});
