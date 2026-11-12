import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { test } from './fixtures';

// TEMPORARY measurement pass for the 2026-11-12 UI audit report (delete after the report is approved).
// Read-only: loads owner screens at three widths and records overflow, tap-target, accessible-name, contrast,
// focus-ring and reduced-motion facts into <SCREENS_DIR>/audit/report.json. Runs after screens-capture.spec.ts
// (file-name order) so the dashboards that spec prepares exist.

const OUT = path.resolve(__dirname, '../..', process.env.SCREENS_DIR ?? '.taste-skill/before', 'audit');
const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '768', width: 768, height: 1024 },
  { name: '390', width: 390, height: 844 },
] as const;
const ROUTES: [string, string][] = [
  ['dashboard', '/dashboard'], ['chat', '/chat'],
  ['settings-sources', '/settings/sources'], ['settings-sources-mcp', '/settings/sources/mcp'],
  ['settings-sources-permissions', '/settings/sources/permissions'], ['settings-models', '/settings/models'],
  ['settings-ai', '/settings/ai'], ['settings-dashboard', '/settings/dashboard'], ['settings-prompts', '/settings/prompts'],
  ['settings-web-search', '/settings/web-search'], ['settings-translation', '/settings/translation'],
  ['settings-memory', '/settings/memory'], ['settings-privacy', '/settings/privacy'],
  ['settings-workspace', '/settings/workspace'], ['settings-n8n', '/settings/n8n'], ['settings-system', '/settings/system'],
  ['settings-global', '/settings/global'],
];

/** Runs in the page: layout, target-size, name and contrast facts for the current viewport. */
function measure() {
  const vw = window.innerWidth;
  const describe = (el: Element) => {
    const text = (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40);
    const cls = (el.getAttribute('class') ?? '').split(/\s+/).slice(0, 4).join('.');
    return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${cls ? `.${cls}` : ''}${text ? ` "${text}"` : ''}`;
  };
  const visible = (el: Element) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0
      && r.bottom > 0 && r.top < window.innerHeight * 4;
  };
  const all = [...document.querySelectorAll('body *')].filter(visible);

  // Horizontal overflow: the document itself, elements poking past the viewport, and inner horizontal scrollers.
  const doc = document.documentElement;
  const clipped = (el: Element) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o !== 'visible') return o;
    }
    return 'none';
  };
  const past = all.filter((el) => el.getBoundingClientRect().right > vw + 1 || el.getBoundingClientRect().left < -1);
  const scrollers = all.filter((el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX) && el.scrollWidth > el.clientWidth + 1);

  // Tap targets: interactive elements smaller than 44px (and the WCAG 2.5.8 floor of 24px) on either axis.
  const interactive = [...document.querySelectorAll('a[href], button, [role="button"], input:not([type="hidden"]), select, textarea, [role="tab"], [role="menuitem"], [role="switch"], [role="checkbox"], [role="radio"], summary')].filter(visible);
  const size = (el: Element) => { const r = el.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)] as const; };
  const small = interactive.filter((el) => { const [w, h] = size(el); return w < 44 || h < 44; });
  const tiny = interactive.filter((el) => { const [w, h] = size(el); return w < 24 || h < 24; });

  // Accessible name: no text, aria-label, aria-labelledby, title, or labelled image/svg inside.
  const nameless = interactive.filter((el) => {
    if (!['A', 'BUTTON'].includes(el.tagName) && el.getAttribute('role') !== 'button') return false;
    if ((el.textContent ?? '').trim()) return false;
    if (el.getAttribute('aria-label')?.trim() || el.getAttribute('aria-labelledby') || el.getAttribute('title')?.trim()) return false;
    return !el.querySelector('img[alt]:not([alt=""]), svg[aria-label], svg title');
  });
  const unlabelledInputs = [...document.querySelectorAll('input:not([type="hidden"]), select, textarea')].filter(visible).filter((el) => {
    const input = el as HTMLInputElement;
    return !input.labels?.length && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') && !el.getAttribute('title');
  });

  // Contrast of text against its effective background (first opaque ancestor); colors normalised through a canvas.
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  const rgba = (color: string) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#000';
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3] / 255] as const;
  };
  const over = (top: readonly number[], under: readonly number[]) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3]));
  const lum = (c: number[]) => {
    const [r, g, b] = c.map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const background = (el: Element): { color: number[]; image: boolean } => {
    const layers: (readonly number[])[] = [];
    let image = false;
    for (let p: Element | null = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.backgroundImage !== 'none') image = true;
      const c = rgba(s.backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] === 1) break; }
    }
    let color = [255, 255, 255];
    for (const layer of layers.reverse()) color = over(layer, color);
    return { color, image };
  };
  const hex = (c: number[]) => `#${c.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')}`;
  const pairs = new Map<string, { fg: string; bg: string; ratio: number; large: boolean; count: number; sample: string }>();
  for (const el of all) {
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim().length > 0);
    if (!own) continue;
    const s = getComputedStyle(el);
    const bg = background(el);
    if (bg.image) continue;
    const fg = over(rgba(s.color), bg.color);
    const [a, b] = [lum(fg), lum(bg.color)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    const px = parseFloat(s.fontSize);
    const large = px >= 24 || (px >= 18.66 && Number(s.fontWeight) >= 700);
    if (ratio >= (large ? 3 : 4.5)) continue;
    const key = `${hex(fg)}|${hex(bg.color)}|${large}`;
    const hit = pairs.get(key);
    if (hit) hit.count += 1;
    else pairs.set(key, { fg: hex(fg), bg: hex(bg.color), ratio: Math.round(ratio * 100) / 100, large, count: 1, sample: describe(el) });
  }

  const tables = [...document.querySelectorAll('table')].filter(visible).map((t) => ({
    display: getComputedStyle(t).display, columns: t.querySelectorAll('thead th').length, rows: t.querySelectorAll('tbody tr').length,
    width: Math.round(t.getBoundingClientRect().width), scrollsX: clipped(t), caption: (t.querySelector('caption')?.textContent ?? '').trim().slice(0, 40),
  }));
  const fontSizes: Record<string, number> = {};
  for (const el of all) {
    if (![...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim())) continue;
    const k = getComputedStyle(el).fontSize;
    fontSizes[k] = (fontSizes[k] ?? 0) + 1;
  }
  return {
    viewport: vw,
    docOverflowPx: doc.scrollWidth - doc.clientWidth,
    bodyOverflowPx: document.body.scrollWidth - document.body.clientWidth,
    pastViewport: past.slice(0, 10).map((el) => ({ el: describe(el), right: Math.round(el.getBoundingClientRect().right), clippedBy: clipped(el) })),
    pastViewportCount: past.length,
    horizontalScrollers: scrollers.slice(0, 10).map((el) => ({ el: describe(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth })),
    interactiveCount: interactive.length,
    under44Count: small.length,
    under44: small.slice(0, 25).map((el) => ({ el: describe(el), size: size(el) })),
    under24Count: tiny.length,
    under24: tiny.slice(0, 15).map((el) => ({ el: describe(el), size: size(el) })),
    namelessControls: nameless.slice(0, 20).map((el) => `${describe(el)} :: ${el.outerHTML.slice(0, 160)}`),
    namelessCount: nameless.length,
    unlabelledInputs: unlabelledInputs.slice(0, 15).map(describe),
    contrastFailures: [...pairs.values()].sort((x, y) => y.count - x.count).slice(0, 20),
    tables,
    fontSizes,
    headings: [...document.querySelectorAll('h1, h2, h3, h4')].filter(visible).slice(0, 40).map((h) => `${h.tagName} ${getComputedStyle(h).fontSize}/${getComputedStyle(h).fontWeight} "${(h.textContent ?? '').trim().slice(0, 40)}"`),
  };
}

/** Runs in the page: how the currently focused element shows focus. */
function focusState() {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const s = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    el: `${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').split(/\s+/).slice(0, 3).join('.')} "${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 30)}"`,
    outline: `${s.outlineStyle} ${s.outlineWidth} ${s.outlineColor}`,
    boxShadow: s.boxShadow.slice(0, 90),
    indicator: (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none',
    onScreen: r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth,
  };
}

/** Runs in the page: motion still active while reduced motion is requested. */
function motionState() {
  const moving = [...document.querySelectorAll('body *')].filter((el) => {
    const s = getComputedStyle(el);
    return s.transitionDuration.split(',').some((d) => parseFloat(d) > 0.02) && s.transitionProperty !== 'none';
  });
  const describe = (el: Element) => `${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').split(/\s+/).slice(0, 4).join('.')}`;
  const running = document.getAnimations().filter((a) => a.playState === 'running');
  return {
    transitionsCount: moving.length,
    transitions: [...new Set(moving.map(describe))].slice(0, 15),
    runningAnimations: running.length,
    animations: running.slice(0, 10).map((a) => { const t = (a.effect as KeyframeEffect | null)?.target; return `${t ? describe(t) : '?'} ${(a as CSSAnimation).animationName ?? ''}`; }),
  };
}

test.use({ trace: 'off', screenshot: 'off', video: 'off' });

test('ui audit measurements', async ({ page }) => {
  test.setTimeout(30 * 60_000);
  mkdirSync(OUT, { recursive: true });
  const report: Record<string, unknown> = {};
  const save = () => writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
  const routes = [...ROUTES];
  await page.goto('/dashboard');
  const dashboards = await (await page.request.get('/api/v1/dashboards')).json().catch(() => []) as { id: string; name: string }[];
  for (const d of dashboards.slice(0, 6)) routes.push([`dash-${d.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, `/dashboard/${d.id}`]);

  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'no-preference' });
    for (const vp of VIEWPORTS) {
      // Dark is only needed for contrast, which does not depend on width.
      if (theme === 'dark' && vp.name !== '1440') continue;
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const [name, route] of routes) {
        try {
          await page.goto(route, { waitUntil: 'load' });
          await page.waitForTimeout(name.startsWith('dash') ? 6000 : 2500);
          report[`${theme}/${vp.name}/${name}`] = await page.evaluate(measure);
        } catch (error) { report[`${theme}/${vp.name}/${name}`] = { error: String(error).split('\n')[0] }; }
        save();
      }
    }
  }

  // Keyboard: Tab through the first 40 stops of each main area and record how focus is shown.
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const [name, route] of [['dashboard', '/dashboard'], ['chat', '/chat'], ['settings-sources', '/settings/sources'], ['settings-ai', '/settings/ai']] as const) {
    try {
      await page.goto(route, { waitUntil: 'load' });
      await page.waitForTimeout(3000);
      const stops: unknown[] = [];
      for (let i = 0; i < 40; i += 1) {
        await page.keyboard.press('Tab');
        stops.push(await page.evaluate(focusState));
        if (i === 2 || i === 12) await page.screenshot({ path: `${OUT}/focus-${name}-${i}.png` }).catch(() => undefined);
      }
      report[`focus/${name}`] = stops;
    } catch (error) { report[`focus/${name}`] = { error: String(error).split('\n')[0] }; }
    save();
  }

  // Reduced motion: what still transitions or animates when the user asks for less motion.
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  for (const [name, route] of [['dashboard', '/dashboard'], ['chat', '/chat'], ['settings-sources', '/settings/sources']] as const) {
    try {
      await page.goto(route, { waitUntil: 'load' });
      await page.waitForTimeout(5000);
      report[`reduced-motion/${name}`] = await page.evaluate(motionState);
    } catch (error) { report[`reduced-motion/${name}`] = { error: String(error).split('\n')[0] }; }
    save();
  }
});
