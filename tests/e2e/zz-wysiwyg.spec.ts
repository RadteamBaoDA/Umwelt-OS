import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test } from './fixtures';
import { writeHeaders } from './sources-helpers';

// TEMPORARY evidence pass for the 2026-11-12 UI audit (delete after the report is approved).
// Drives the real dashboard editor to check WYSIWYG claims that were only read from code: view vs edit geometry at
// three widths, resize -> save -> reload fidelity, remove -> discard, the gadget editor, the preset preview, and a
// populated chat conversation at three widths. Every step is isolated; a failed step is recorded, never fatal.

const OUT = path.resolve(__dirname, '../..', process.env.SCREENS_DIR ?? '.taste-skill/ui-audit', 'wysiwyg');
const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '768', width: 768, height: 1024 },
  { name: '390', width: 390, height: 844 },
] as const;
const report: Record<string, unknown> = {};
// WYS_RERUN=1 repeats only the layout checks (A, B, E) and keeps the first run's report.
const RERUN = Boolean(process.env.WYS_RERUN);
const save = () => { mkdirSync(OUT, { recursive: true }); writeFileSync(`${OUT}/${RERUN ? 'report-rerun' : 'report'}.json`, JSON.stringify(report, null, 2)); };
/** Compact screens hide gadgets behind a Map | Gadgets switch; open the gadgets side when it exists. */
const showGadgets = async (page: Page) => {
  const tab = page.getByRole('tab', { name: 'Gadgets' });
  if (await tab.count()) { await tab.first().click().catch(() => undefined); await page.waitForTimeout(1500); }
};
const shot = async (page: Page, name: string) => { mkdirSync(OUT, { recursive: true }); await page.screenshot({ path: `${OUT}/${name}.png`, animations: 'disabled' }).catch(() => undefined); };
const firstLine = (error: unknown) => String(error).split('\n')[0];

/** Runs in the page: every gadget region with its on-screen box and its grid placement. */
function gadgetRects() {
  return [...document.querySelectorAll('main [role="region"]')].filter((r) => r.querySelector('h3')).map((region, index) => {
    const rect = region.getBoundingClientRect();
    const cell = region.closest('[style*="grid-column"]') as HTMLElement | null;
    return {
      index, title: (region.querySelector('h3')?.textContent ?? '').trim(),
      x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height),
      gridColumn: cell?.style.gridColumn ?? null, gridRow: cell?.style.gridRow ?? null,
      clipped: [...region.querySelectorAll('*')].filter((el) => el.scrollHeight > el.clientHeight + 4 && getComputedStyle(el).overflowY === 'hidden').length,
    };
  });
}

/** Runs in the page: layout facts for a populated chat conversation. */
function chatFacts() {
  const vw = window.innerWidth;
  const visible = (el: Element) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const all = [...document.querySelectorAll('main *')].filter(visible);
  const label = (el: Element) => `${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').split(/\s+/).slice(0, 3).join('.')}`;
  const interactive = [...document.querySelectorAll('main a[href], main button, main [role="button"], main textarea, main input')].filter(visible);
  return {
    docOverflowPx: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    horizontalScrollers: all.filter((el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX) && el.scrollWidth > el.clientWidth + 1)
      .slice(0, 8).map((el) => ({ el: label(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth })),
    pastViewport: all.filter((el) => el.getBoundingClientRect().right > vw + 1).length,
    tables: [...document.querySelectorAll('main table')].filter(visible).map((t) => ({ display: getComputedStyle(t).display, width: Math.round(t.getBoundingClientRect().width), columns: t.querySelectorAll('thead th').length })),
    codeBlocks: [...document.querySelectorAll('main pre')].filter(visible).map((p) => ({ scrollWidth: p.scrollWidth, clientWidth: p.clientWidth })),
    messageWidths: [...document.querySelectorAll('[data-role="assistant"], [data-role="user"]')].filter(visible).slice(0, 6).map((m) => Math.round(m.getBoundingClientRect().width)),
    under24: interactive.filter((el) => { const r = el.getBoundingClientRect(); return r.width < 24 || r.height < 24; }).slice(0, 12)
      .map((el) => `${label(el)} "${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 30)}" ${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`),
    nameless: interactive.filter((el) => ['A', 'BUTTON'].includes(el.tagName) && !(el.textContent ?? '').trim() && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') && !el.getAttribute('title'))
      .slice(0, 12).map((el) => el.outerHTML.slice(0, 140)),
    interactiveCount: interactive.length,
  };
}

test.use({ trace: 'off', screenshot: 'off', video: 'off' });
test.describe.configure({ mode: 'serial' });

let dashboardId = '';
let dashboardName = '';

test('prepare: preset dashboards', async ({ page }) => {
  test.setTimeout(5 * 60_000);
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
  const dashboards = await (await page.request.get('/api/v1/dashboards')).json() as { id: string; name: string }[];
  // Pick the dashboard with the most gadgets so placement differences are visible.
  let best = 0;
  for (const d of dashboards) {
    await page.goto(`/dashboard/${d.id}`, { waitUntil: 'load' });
    await page.waitForTimeout(2500);
    const count = (await page.evaluate(gadgetRects)).length;
    if (count > best) { best = count; dashboardId = d.id; dashboardName = d.name; }
  }
  report.dashboard = { id: dashboardId, name: dashboardName, gadgets: best, all: dashboards.map((d) => d.name) };
  save();
});

test('A: view mode vs edit mode geometry at three widths', async ({ page }) => {
  test.setTimeout(6 * 60_000);
  for (const vp of VIEWPORTS) {
    try {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`/dashboard/${dashboardId}`, { waitUntil: 'load' });
      await page.waitForTimeout(5000);
      await showGadgets(page);
      const view = await page.evaluate(gadgetRects);
      await shot(page, `A-${vp.name}-view`);
      // Desktop only: the same view with the fixed map panel collapsed, where the gadget panel gets the full width.
      let viewCollapsed: unknown = null;
      const collapse = page.getByRole('button', { name: 'Collapse map' });
      if (await collapse.count()) {
        await collapse.click();
        await page.waitForTimeout(2500);
        viewCollapsed = await page.evaluate(gadgetRects);
        await shot(page, `A-${vp.name}-view-map-collapsed`);
        await page.getByRole('button', { name: 'Expand map' }).click().catch(() => undefined);
        await page.waitForTimeout(1000);
      }
      await page.getByRole('button', { name: 'Edit layout' }).click();
      await page.waitForTimeout(2000);
      const edit = await page.evaluate(gadgetRects);
      await shot(page, `A-${vp.name}-edit`);
      report[`A/${vp.name}`] = { view, viewCollapsed, edit, toolbar: await page.getByRole('button').allTextContents().then((t) => t.map((s) => s.trim()).filter(Boolean).slice(0, 30)) };
      await page.getByRole('button', { name: 'Cancel', exact: true }).click().catch(() => undefined);
    } catch (error) { report[`A/${vp.name}`] = { error: firstLine(error) }; }
    save();
  }
});

test('B: resize a gadget, save, reload', async ({ page }) => {
  test.setTimeout(4 * 60_000);
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/dashboard/${dashboardId}`, { waitUntil: 'load' });
    await page.waitForTimeout(5000);
    const before = await page.evaluate(gadgetRects);
    await page.getByRole('button', { name: 'Edit layout' }).click();
    await page.waitForTimeout(1500);
    const editBefore = await page.evaluate(gadgetRects);
    // South resize handle of the first gadget: the real pointer gesture an owner uses.
    const cell = page.locator('main [role="region"]:has(h3)').nth(2);
    // Each gadget has three handles (width, height, corner); index 1 is the first gadget's height handle.
    const handles = page.locator('main [role="separator"]');
    const handleCount = await handles.count();
    let gesture = 'none';
    if (handleCount >= 2) {
      await handles.nth(1).scrollIntoViewIfNeeded().catch(() => undefined);
      const box = await handles.nth(1).boundingBox();
      if (box) {
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 + 60, { steps: 6 });
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 + 130, { steps: 6 });
        await shot(page, 'B-1440-mid-resize');
        await page.mouse.up();
        gesture = 'pointer-south';
      }
    }
    await page.waitForTimeout(800);
    let editAfter = await page.evaluate(gadgetRects);
    if (JSON.stringify(editAfter) === JSON.stringify(editBefore)) {
      // Fallback: the documented keyboard resize (Shift+ArrowDown) on the focused cell.
      await cell.locator('h3').first().click().catch(() => undefined);
      await page.keyboard.press('Shift+ArrowDown');
      await page.keyboard.press('Shift+ArrowDown');
      await page.waitForTimeout(600);
      editAfter = await page.evaluate(gadgetRects);
      gesture = `${gesture}+keyboard`;
    }
    await shot(page, 'B-1440-edit-after-resize');
    const saveButton = page.getByRole('button', { name: 'Save layout' });
    const saveEnabled = await saveButton.isEnabled().catch(() => false);
    if (saveEnabled) await saveButton.click();
    await page.waitForTimeout(3000);
    await page.getByRole('button', { name: 'Done editing' }).click().catch(() => undefined);
    await page.waitForTimeout(2000);
    const savedView = await page.evaluate(gadgetRects);
    await shot(page, 'B-1440-view-after-save');
    await page.reload({ waitUntil: 'load' });
    await page.waitForTimeout(5000);
    const reloaded = await page.evaluate(gadgetRects);
    await shot(page, 'B-1440-view-after-reload');
    report.B = { gesture, handleCount, saveEnabled, before, editBefore, editAfter, savedView, reloaded };
    // Same saved layout at the other widths, to compare against what was arranged on desktop.
    for (const vp of VIEWPORTS.slice(1)) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.reload({ waitUntil: 'load' });
      await page.waitForTimeout(5000);
      await showGadgets(page);
      await shot(page, `B-${vp.name}-view-after-reload`);
      (report.B as Record<string, unknown>)[`reloaded${vp.name}`] = await page.evaluate(gadgetRects);
    }
  } catch (error) { report.B = { ...(report.B as object ?? {}), error: firstLine(error) }; }
  save();
});

test('C: remove a gadget, then cancel/discard and reload', async ({ page }) => {
  test.skip(RERUN, 'recorded in the first run');
  test.setTimeout(4 * 60_000);
  const count = () => page.locator('main [role="region"]:has(h3)').count();
  const out: Record<string, unknown> = {};
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/dashboard/${dashboardId}`, { waitUntil: 'load' });
    await page.waitForTimeout(5000);
    out.before = await count();
    await page.getByRole('button', { name: 'Edit layout' }).click();
    await page.waitForTimeout(1500);
    const region = page.locator('main [role="region"]:has(h3)').last();
    out.removedTitle = (await region.locator('h3').first().textContent())?.trim();
    await region.scrollIntoViewIfNeeded();
    await region.getByRole('button', { name: 'Configure gadget' }).first().click();
    await page.waitForTimeout(400);
    await shot(page, 'C-1440-gadget-menu');
    const requests: string[] = [];
    page.on('request', (r) => { if (r.method() !== 'GET' && /\/api\/v1\//.test(r.url())) requests.push(`${r.method()} ${new URL(r.url()).pathname}`); });
    await page.getByRole('menuitem', { name: 'Remove gadget' }).click();
    await page.waitForTimeout(1500);
    out.confirmDialogShown = await page.locator('[role="alertdialog"], [role="dialog"]').count();
    await shot(page, 'C-1440-after-remove-click');
    if (out.confirmDialogShown) {
      out.confirmText = (await page.locator('[role="alertdialog"], [role="dialog"]').first().textContent())?.trim().slice(0, 200);
      await page.locator('[role="alertdialog"], [role="dialog"]').first().getByRole('button', { name: /remove|delete|confirm/i }).first().click().catch(() => undefined);
      await page.waitForTimeout(1500);
    }
    out.afterRemove = await count();
    out.requestsOnRemove = [...requests];
    out.undoEnabled = await page.getByRole('button', { name: 'Undo' }).isEnabled().catch(() => null);
    out.saveEnabled = await page.getByRole('button', { name: 'Save layout' }).isEnabled().catch(() => null);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click().catch(() => undefined);
    await page.waitForTimeout(800);
    const discard = page.getByRole('button', { name: 'Discard changes' });
    out.discardPromptShown = await discard.count();
    if (out.discardPromptShown) await discard.click();
    await page.waitForTimeout(2000);
    out.afterCancel = await count();
    await page.reload({ waitUntil: 'load' });
    await page.waitForTimeout(5000);
    out.afterReload = await count();
    await shot(page, 'C-1440-after-cancel-reload');
  } catch (error) { out.error = firstLine(error); }
  report.C = out;
  save();
});

test('D: gadget editor shows what while configuring', async ({ page }) => {
  test.skip(RERUN, 'recorded in the first run');
  test.setTimeout(4 * 60_000);
  for (const vp of [VIEWPORTS[0], VIEWPORTS[2]]) {
    const out: Record<string, unknown> = {};
    try {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`/dashboard/${dashboardId}`, { waitUntil: 'load' });
      await page.waitForTimeout(5000);
      await page.getByRole('button', { name: 'Edit layout' }).click();
      await page.waitForTimeout(1500);
      const region = page.locator('main [role="region"]:has(h3)').first();
      out.gadget = (await region.locator('h3').first().textContent())?.trim();
      await region.getByRole('button', { name: 'Configure gadget' }).first().click();
      await page.getByRole('menuitem', { name: 'Configure gadget' }).click();
      const dialog = page.getByRole('dialog').first();
      await dialog.waitFor({ timeout: 15_000 });
      await page.waitForTimeout(1500);
      await shot(page, `D-${vp.name}-gadget-editor`);
      out.dialog = await dialog.evaluate((d) => ({
        fields: d.querySelectorAll('input, select, textarea, [role="combobox"], [role="checkbox"], [role="radio"], [role="switch"]').length,
        liveRegions: d.querySelectorAll('[role="region"]').length, charts: d.querySelectorAll('.recharts-wrapper, canvas').length,
        text: (d.textContent ?? '').replace(/\s+/g, ' ').slice(0, 400), scrollsX: d.scrollWidth > d.clientWidth + 1,
        width: Math.round(d.getBoundingClientRect().width), viewport: window.innerWidth,
      }));
      await page.keyboard.press('Escape');
    } catch (error) { out.error = firstLine(error); }
    report[`D/${vp.name}`] = out;
    save();
  }
});

test('E: preset preview vs applied result', async ({ page }) => {
  test.setTimeout(4 * 60_000);
  const out: Record<string, unknown> = {};
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/dashboard/${dashboardId}`, { waitUntil: 'load' });
    await page.waitForTimeout(4000);
    await page.getByRole('button', { name: 'Edit layout' }).click();
    await page.getByRole('button', { name: 'Presets' }).click();
    const picker = page.getByRole('dialog').filter({ hasText: 'Apply preset' });
    await picker.waitFor({ timeout: 30_000 });
    await page.waitForTimeout(1500);
    // Nothing is selected by default, so Apply stays disabled until a preset is chosen.
    await picker.getByRole('button', { name: /^Finance/ }).first().click();
    await page.waitForTimeout(4000);
    await shot(page, 'E-1440-preset-preview');
    out.pickerText = (await picker.textContent())?.replace(/\s+/g, ' ').slice(0, 500);
    out.pressed = await picker.locator('button[aria-pressed="true"]').allTextContents();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(1200);
    await shot(page, 'E-390-preset-preview');
    await page.setViewportSize({ width: 1440, height: 900 });
    const before = await page.getByRole('tab').count();
    await picker.getByRole('button', { name: 'Apply preset' }).click();
    await picker.waitFor({ state: 'hidden', timeout: 60_000 });
    await page.waitForTimeout(5000);
    out.tabsBefore = before;
    out.tabsAfter = await page.getByRole('tab').count();
    out.applied = await page.evaluate(gadgetRects);
    await shot(page, 'E-1440-preset-applied');
  } catch (error) { out.error = firstLine(error); }
  report.E = out;
  save();
});

test('G: populated chat conversation at three widths', async ({ page }) => {
  test.skip(RERUN, 'recorded in the first run');
  test.setTimeout(5 * 60_000);
  const conversations = await (await page.request.get('/api/v1/conversations?limit=20')).json().catch(() => null) as { items?: { id: string; title?: string }[] } | { id: string; title?: string }[] | null;
  const list = Array.isArray(conversations) ? conversations : conversations?.items ?? [];
  report['G/conversations'] = list.map((c) => c.title ?? c.id).slice(0, 10);
  for (const [index, conversation] of list.slice(0, 3).entries()) {
    for (const vp of VIEWPORTS) {
      try {
        await page.setViewportSize({ width: vp.width, height: vp.height });
        await page.goto(`/chat/${conversation.id}`, { waitUntil: 'load' });
        await page.waitForTimeout(4000);
        await shot(page, `G-chat${index}-${vp.name}`);
        report[`G/chat${index}/${vp.name}`] = await page.evaluate(chatFacts);
      } catch (error) { report[`G/chat${index}/${vp.name}`] = { error: firstLine(error) }; }
      save();
    }
  }
});
