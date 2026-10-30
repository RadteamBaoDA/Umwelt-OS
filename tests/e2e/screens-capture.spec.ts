import { mkdirSync, writeFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { openWizard, writeHeaders } from './sources-helpers';

/**
 * Design-review capture, not a behavioural test: screenshots every owner screen at three widths in both themes,
 * plus the main overlays and the signed-out pages, into SCREENS_DIR (default .taste-skill/before).
 */
const OUT = process.env.SCREENS_DIR ?? '.taste-skill/before';
const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '768', width: 768, height: 1024 },
  { name: '390', width: 390, height: 844 },
] as const;
const THEMES = ['light', 'dark'] as const;
const ROUTES: [string, string][] = [
  ['dashboard', '/dashboard'],
  ['chat', '/chat'],
  ['settings-sources', '/settings/sources'],
  ['settings-sources-mcp', '/settings/sources/mcp'],
  ['settings-sources-permissions', '/settings/sources/permissions'],
  ['settings-models', '/settings/models'],
  ['settings-ai', '/settings/ai'],
  ['settings-dashboard', '/settings/dashboard'],
  ['settings-prompts', '/settings/prompts'],
  ['settings-web-search', '/settings/web-search'],
  ['settings-translation', '/settings/translation'],
  ['settings-memory', '/settings/memory'],
  ['settings-privacy', '/settings/privacy'],
  ['settings-workspace', '/settings/workspace'],
  ['settings-n8n', '/settings/n8n'],
  ['settings-system', '/settings/system'],
  ['settings-global', '/settings/global'],
  ['knowledge-documents', '/knowledge/documents'],
  ['knowledge-entities', '/knowledge/entities'],
  ['knowledge-memory', '/knowledge/memory'],
  ['news', '/news'],
  ['search', '/search'],
  ['timeline', '/timeline'],
  ['sources', '/sources'],
  ['pick', '/pick'],
  ['onboarding', '/onboarding'],
];
const PUBLIC_ROUTES: [string, string][] = [
  ['login', '/login'], ['setup', '/setup'], ['privacy', '/privacy'], ['terms', '/terms'], ['refunds', '/refunds'], ['invite', '/invite'],
];

/** Loads a route and lets client data settle; realtime streams keep the network busy, so no networkidle wait. */
async function settle(page: Page, path: string) {
  await page.goto(path, { waitUntil: 'load' });
  await page.locator('main, body').first().waitFor({ state: 'visible' });
  await page.waitForTimeout(1500);
}

/** Saves one viewport screenshot; a failure to capture one screen never stops the rest of the sweep. */
async function shot(page: Page, dir: string, name: string, fullPage = false) {
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/${name}.png`, animations: 'disabled', fullPage }).catch(() => undefined);
}

/**
 * Whole-dashboard capture: the shell scrolls inside an inner container, so `fullPage` stops at the viewport. Grows the
 * viewport to the tallest scroll height on the page (capped), shoots, then restores it.
 */
async function shotTall(page: Page, dir: string, name: string) {
  const size = page.viewportSize()!;
  const height = await page.evaluate(() => Math.max(...[...document.querySelectorAll('*')].map((el) => el.scrollHeight)));
  await page.setViewportSize({ width: size.width, height: Math.min(Math.max(height, size.height), 12_000) });
  await page.waitForTimeout(2000);
  await shot(page, dir, name, true);
  await page.setViewportSize(size);
}

test.describe.configure({ mode: 'serial' });

test('prepare: a preset dashboard so the dashboard is not only its empty state', async ({ page }) => {
  await page.goto('/dashboard');
  const presets = await (await page.request.get('/api/v1/dashboard-presets')).json() as { id: string }[];
  const headers = await writeHeaders(page);
  for (const preset of presets) {
    const preview = await page.request.post(`/api/v1/dashboard-presets/${preset.id}/preview`, { headers, data: {} });
    if (!preview.ok()) continue;
    const body = await preview.json() as Record<string, unknown>;
    const fingerprint = (body.fingerprint ?? body.preview_fingerprint) as string | undefined;
    if (fingerprint) await page.request.post(`/api/v1/dashboard-presets/${preset.id}/apply`, { headers, data: { preview_fingerprint: fingerprint, mode: 'create' } });
  }
});

type Template = { id: string; renderer: string; source_mode: 'none' | 'single' | 'multi'; accepted_shapes: string[] | null };
type SourceOption = { id: string; provider: string | null; status: string; data_shapes: string[] };
type Catalog = { metrics: { id: string }[]; symbol_keyed: boolean; suggested_symbols: string[]; region_keyed: boolean; suggested_regions: string[] };
/** Preferred providers per renderer so each type shows its most legible data; any compatible source is the fallback. */
const PREFERRED: Record<string, string[]> = {
  map: ['usgs', 'emsc_quakes', 'gdacs_events', 'nasa_eonet'], intelligence_panel: ['usgs', 'gdacs_events', 'nasa_eonet'],
  finance_chart: ['binance', 'alpha_vantage', 'coinpaprika'], provider_observation: ['alternative_me', 'binance', 'open_er_api'],
  weather: ['open_meteo'], news_feed: ['bbc_world', 'npr_news', 'hn_top'], table_panel: ['usgs', 'launch_library', 'nvd'],
  video_panel: ['youtube'], text_panel: ['bbc_world'],
};

/** Higher is better: a preferred provider by its order, then any active source. */
function rank(renderer: string, source: SourceOption): number {
  const order = PREFERRED[renderer] ?? [];
  const index = source.provider ? order.indexOf(source.provider) : -1;
  return (index >= 0 ? 100 - index : 0) + (source.status === 'active' ? 1 : 0);
}

/**
 * Builds one "All gadget types" dashboard: one gadget per built-in template, bound to the best compatible enabled
 * source (with the provider's first metric / suggested symbol where the renderer needs one). A template no source can
 * satisfy is still added unbound, so every type appears in the capture with its real setup state.
 */
test('prepare: a dashboard with every gadget type', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await page.goto('/dashboard');
  const headers = () => writeHeaders(page);
  const templates = await (await page.request.get('/api/v1/gadget-templates')).json() as Template[];
  const catalog = await (await page.request.get('/api/v1/gadget-metric-catalog')).json() as Record<string, Catalog>;
  const sources = ((await (await page.request.get('/api/v1/gadget-sources?limit=100')).json()) as { items: SourceOption[] }).items;
  const created = await page.request.post('/api/v1/dashboards', { headers: await headers(), data: { name: 'All gadget types' } });
  expect(created.ok(), await created.text()).toBe(true);
  let dashboard = await created.json() as { id: string; revision: number };
  // The map renders a 3D Earth globe by default; a second map pins the flat engine so both are captured.
  const plan: (Template & { title: string; engine?: 'globe' | 'flat' })[] = templates.flatMap((t) => t.renderer === 'map'
    ? [{ ...t, title: 'map earth', engine: 'globe' as const }, { ...t, title: 'map flat', engine: 'flat' as const }]
    : [{ ...t, title: t.id.replace(/_/g, ' ') }]);
  for (const template of plan) {
    const compatible = template.source_mode === 'none' ? [] : sources
      .filter((s) => !template.accepted_shapes || s.data_shapes.some((shape) => template.accepted_shapes!.includes(shape)))
      .sort((a, b) => rank(template.renderer, b) - rank(template.renderer, a));
    const attempts: SourceOption[][] = template.source_mode === 'multi'
      ? [compatible.slice(0, 3), ...compatible.slice(0, 5).map((s) => [s])]
      : compatible.slice(0, 6).map((s) => [s]);
    attempts.push([]);
    let definitionId: string | null = null;
    for (const bound of attempts) {
      const meta = bound.length === 1 && bound[0].provider ? catalog[bound[0].provider] : undefined;
      const scope = {
        ...(meta?.metrics.length && ['provider_observation', 'weather', 'finance_chart'].includes(template.renderer) ? { metrics: [meta.metrics[0].id] } : {}),
        ...(meta?.symbol_keyed && meta.suggested_symbols.length ? { symbols: meta.suggested_symbols.slice(0, 1) } : {}),
        ...(meta?.region_keyed && meta.suggested_regions.length ? { regions: meta.suggested_regions.slice(0, 1) } : {}),
      };
      const res = await page.request.post('/api/v1/gadget-definitions', { headers: await headers(), data: {
        name: `All types · ${template.title}`, renderer: template.renderer, source_ids: bound.map((s) => s.id), scope,
        filters: template.renderer === 'map' ? { show_precise_locations: true, map_engine: template.engine } : {},
      } });
      if (res.ok()) { definitionId = (await res.json() as { id: string }).id; break; }
    }
    if (!definitionId) { console.log(`TEMPLATE ${template.id}: no definition accepted`); continue; }
    const placed = await page.request.post(`/api/v1/dashboards/${dashboard.id}/instances`, { headers: await headers(), data: {
      expected_revision: dashboard.revision, definition_id: definitionId, title: template.title,
    } });
    if (placed.ok()) dashboard = await placed.json() as { id: string; revision: number };
    else console.log(`TEMPLATE ${template.id}: placement ${placed.status()} ${await placed.text()}`);
  }
});

for (const theme of THEMES) {
  for (const vp of VIEWPORTS) {
    test(`screens ${theme} ${vp.name}`, async ({ page }) => {
      test.setTimeout(10 * 60_000);
      const dir = `${OUT}/${theme}/${vp.name}`;
      await page.emulateMedia({ colorScheme: theme });
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const [name, path] of ROUTES) {
        await settle(page, path);
        await shot(page, dir, name);
      }
      const dashboards = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string }[];
      // Every dashboard (presets and any kept e2e dashboard), first screen and whole page.
      for (const d of dashboards) {
        const slug = d.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
        await settle(page, `/dashboard/${d.id}`);
        await page.waitForTimeout(3000);
        await shot(page, dir, `dash-${slug}`);
        await shotTall(page, dir, `dash-${slug}-full`);
      }
      // Close-up of every gadget type (light, desktop): each region alone, after data has had time to load.
      const allTypes = dashboards.find((d) => d.name === 'All gadget types');
      if (allTypes && theme === 'light' && vp.name === '1440') {
        // Browser console/page errors while the gadgets render, saved next to the close-ups for diagnosis.
        const consoleLines: string[] = [];
        const onConsole = (m: { type(): string; text(): string }) => { if (['error', 'warning'].includes(m.type())) consoleLines.push(`${m.type()}: ${m.text()}`); };
        const onPageError = (e: Error) => consoleLines.push(`pageerror: ${e.message}`);
        page.on('console', onConsole);
        page.on('pageerror', onPageError);
        await settle(page, `/dashboard/${allTypes.id}`);
        await page.waitForTimeout(15_000);
        mkdirSync(`${OUT}/gadgets`, { recursive: true });
        // Maps mount their engine only while visible, so canvases are inspected after each region is scrolled in.
        const mapDiag: unknown[] = [];
        const regions = page.locator('main [role="region"]:has(h3)');
        for (let i = 0; i < await regions.count(); i += 1) {
          const region = regions.nth(i);
          const title = ((await region.locator('h3').first().textContent()) ?? `gadget-${i}`).trim().toLowerCase().replace(/[^a-z0-9]+/g, '-');
          await region.scrollIntoViewIfNeeded().catch(() => undefined);
          await page.waitForTimeout(3000);
          await region.screenshot({ path: `${OUT}/gadgets/${title}.png`, animations: 'disabled' }).catch(() => undefined);
          if (title.startsWith('map')) {
            mapDiag.push({ title, canvases: await region.evaluate((r) => [...r.querySelectorAll('canvas')].map((c) => {
              const rect = c.getBoundingClientRect();
              return { css: [Math.round(rect.width), Math.round(rect.height)], backing: [c.width, c.height] };
            })), status: await region.locator('[role="status"]').allTextContents() });
          }
        }
        page.off('console', onConsole);
        page.off('pageerror', onPageError);
        writeFileSync(`${OUT}/gadgets/console.txt`, consoleLines.join('\n'));
        writeFileSync(`${OUT}/gadgets/map-diagnosis.json`, JSON.stringify(mapDiag, null, 2));
      }
      if (dashboards[0]) {
        await settle(page, `/dashboard/${dashboards[0].id}`);
        await shot(page, dir, 'dashboard-detail');
        // Overlays: quick chat sheet, command palette, user menu.
        await page.keyboard.press('Control+j');
        await page.waitForTimeout(800);
        await shot(page, dir, 'overlay-quick-chat');
        await page.keyboard.press('Escape');
      }
      await settle(page, '/dashboard');
      await page.keyboard.press('Control+k');
      await page.waitForTimeout(800);
      await shot(page, dir, 'overlay-command-palette');
      await page.keyboard.press('Escape');
      await settle(page, '/settings/sources');
      await openWizard(page).then(() => page.waitForTimeout(800)).then(() => shot(page, dir, 'overlay-source-wizard')).catch(() => undefined);
      expect(true).toBe(true);
    });
  }
}

test('signed-out screens', async ({ browser }) => {
  test.setTimeout(5 * 60_000);
  for (const theme of THEMES) {
    for (const vp of VIEWPORTS) {
      const context = await browser.newContext({ storageState: undefined, colorScheme: theme, viewport: { width: vp.width, height: vp.height } });
      const page = await context.newPage();
      for (const [name, path] of PUBLIC_ROUTES) {
        await settle(page, path);
        await shot(page, `${OUT}/${theme}/${vp.name}`, `public-${name}`);
      }
      await context.close();
    }
  }
});
