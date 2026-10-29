import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';

// Generative UI port (real browser, real built web app). The API is mocked with page.route (same pattern as
// chat-web-search.spec.ts); what is NOT mocked and is under test: the iframe sandbox attribute, the srcdoc CSP,
// the postMessage bridge (resize + sendPrompt), module loading from /vendor/genui/ and its CORS/cache headers.

const NOW = '2026-10-19T00:00:00Z';
type Json = Record<string, unknown>;

const TABLE_UI: Json = {
  kind: 'table', status: 'completed', title: 'Quarterly revenue',
  table: {
    title: 'Quarterly revenue',
    columns: [{ key: 'quarter', label: 'Quarter' }, { key: 'revenue', label: 'Revenue' }],
    rows: [{ quarter: 'Q1', revenue: 100 }, { quarter: 'Q2', revenue: 140 }],
  },
};

/** Sandbox payload: tall content (resize), a button calling sendPrompt, and a marker proving scripts ran. */
const SANDBOX_UI: Json = {
  kind: 'sandboxed_ui', status: 'completed', title: 'Counter',
  sandbox: {
    initialHeight: 120,
    css: '#tall { height: 480px; background: var(--surface); }',
    html: '<div id="tall"><p id="msg">sandbox rendered</p><button id="ask" type="button">Ask more</button></div>',
    jsExpressions: "document.getElementById('msg').textContent = 'js ran'; document.getElementById('ask').addEventListener('click', function () { window.sendPrompt('Tell me more about the counter'); });",
  },
};

/** Sandbox payload importing the vendored d3 module through the importmap. */
const D3_UI: Json = {
  kind: 'sandboxed_ui', status: 'completed', title: 'D3 chart',
  sandbox: {
    initialHeight: 160,
    html: '<div id="chart"></div><p id="status">pending</p>',
    jsFunctions: "async function setup() { try { const d3 = await import('d3'); const svg = d3.select('#chart').append('svg').attr('width', 40).attr('height', 40); svg.append('rect').attr('width', 10 * d3.scaleLinear().domain([0, 1]).range([1, 3])(1)).attr('height', 10); document.getElementById('status').textContent = 'd3 ok ' + typeof d3.scaleLinear; } catch (e) { document.getElementById('status').textContent = 'd3 fail ' + e.message; } }",
    jsExpressions: 'setup();',
  },
};

/**
 * Mocks the chat API for one conversation `c1`. The transcript is stateful: a POSTed user message is appended
 * and, for streamed turns, the assistant reply is persisted so the refetch after `message.done` sees the widget.
 */
async function mockChat(page: Page, opts: { assistantUi?: Json; sse?: string; onSend?: (body: Json) => void } = {}) {
  const conversation = { id: 'c1', title: 'Widget chat', context_kind: null, context_resource_id: null, pinned: false, archived: false, created_at: NOW, updated_at: NOW, metadata: {} };
  const message = (id: string, role: string, content: string, extra: Json = {}) => ({ id, conversation_id: 'c1', role, content, client_request_id: null, model_identity: null, citations: [], response_id: null, revision_of_message_id: null, created_at: NOW, ...extra });
  const messages: Json[] = [message('m1', 'user', 'show me something')];
  if (opts.assistantUi && !opts.sse) messages.push(message('m2', 'assistant', 'Here you go.', { generative_ui: opts.assistantUi, response_id: 'r0' }));
  await page.route('**/api/v1/settings/ai', (route) => route.fulfill({ json: { providers: [{ id: 'default', credential_configured: false, local: true, policy_denied: false, destination_id: null }], defaults: { chat: { model: 'e2e-model', provider_id: 'default' } }, web_search_provider: 'none', web_search_credential_configured: false, privacy: { allow_remote_web_search: false } } }));
  await page.route('**/api/v1/conversations/c1', (route) => route.fulfill({ json: { ...conversation, active_response_id: null, messages } }));
  await page.route(/\/api\/v1\/conversations(\?.*)?$/, (route) => route.fulfill({ json: [conversation] }));
  await page.route('**/api/v1/conversations/c1/messages', async (route) => {
    const body = route.request().postDataJSON() as Json;
    opts.onSend?.(body);
    messages.push(message(`u${messages.length}`, 'user', String(body.content), { response_id: 'r1' }));
    if (opts.sse && opts.assistantUi) messages.push(message('a1', 'assistant', 'Done.', { generative_ui: opts.assistantUi, response_id: 'r1' }));
    await route.fulfill({ status: 202, json: { message_id: 'u-new', response_id: 'r1', status: 'pending' } });
  });
  await page.route('**/api/v1/responses/r1/events**', (route) => opts.sse
    ? route.fulfill({ status: 200, contentType: 'text/event-stream', body: opts.sse })
    : route.fulfill({ status: 500, json: { detail: 'stop' } }));
}

/** Formats server-sent events the way the worker publishes them (`id`, `event`, `data` frames). */
const sse = (frames: Array<[string, Json]>) => frames.map(([event, data], i) => `id: r1:${i + 1}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');

async function openChat(page: Page) {
  await page.goto('/chat');
  // /chat starts with its history rail collapsed; expand it (locale-independent) unless already open.
  const toggle = page.locator('[aria-controls="chat-history-sidebar"]').first();
  await toggle.waitFor();
  if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click();
  await page.getByText('Widget chat').first().click();
}

test.describe('render_table', () => {
  test('a persisted table widget is visible', async ({ page }) => {
    await mockChat(page, { assistantUi: TABLE_UI });
    await openChat(page);
    const widget = page.getByTestId('generative-ui-widget');
    await expect(widget).toBeVisible();
    await expect(widget).toContainText('Quarterly revenue');
    await expect(widget.getByText('Q2')).toBeVisible();
  });

  test('a streamed turn ends with the persisted widget', async ({ page }) => {
    const sent: Json[] = [];
    const stream = sse([
      ['message.ui.delta', { ...TABLE_UI, status: 'streaming' }],
      ['message.delta', { text: 'Done.' }],
      ['message.ui.done', TABLE_UI],
      ['message.done', { text: 'Done.', citations: [], status: 'completed', generative_ui: TABLE_UI }],
    ]);
    await mockChat(page, { assistantUi: TABLE_UI, sse: stream, onSend: (b) => sent.push(b) });
    await openChat(page);
    await page.getByRole('textbox', { name: /Ask a question about your knowledge/ }).fill('table please');
    await page.keyboard.press('Enter');
    await expect.poll(() => sent.length).toBe(1);
    await expect(page.getByTestId('generative-ui-widget')).toContainText('Quarterly revenue');
  });
});

test.describe('render_sandboxed_ui', () => {
  // Tall viewport: the 480 px widget must fit so a real pointer click on the in-frame button is actionable.
  test.use({ viewport: { width: 1280, height: 1600 } });

  test('iframe is sandboxed without same-origin, renders, resizes and bridges sendPrompt exactly once', async ({ page }) => {
    const sent: Json[] = [];
    await mockChat(page, { assistantUi: SANDBOX_UI, onSend: (b) => sent.push(b) });
    await openChat(page);
    const iframe = page.locator('[data-testid="sandboxed-ui-iframe"]');
    await expect(iframe).toBeVisible();
    await expect(iframe).toHaveAttribute('sandbox', 'allow-scripts');
    const frame = page.frameLocator('[data-testid="sandboxed-ui-iframe"]');
    await expect(frame.locator('#msg')).toHaveText('js ran');
    // Resize: the host container grows from initialHeight (120) to the reported content height (>= 480).
    const container = page.getByTestId('sandboxed-ui-container');
    await expect.poll(async () => (await container.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(480);
    await frame.locator('#ask').click();
    await expect.poll(() => sent.length).toBe(1);
    await page.waitForTimeout(500);
    expect(sent).toHaveLength(1);
    expect(sent[0].content).toBe('Tell me more about the counter');
  });

  test('imports vendored d3 from /vendor/genui with no external request and no CSP violation', async ({ page, baseURL }) => {
    const origin = new URL(baseURL!).origin;
    const external: string[] = [];
    const vendor: string[] = [];
    const violations: string[] = [];
    page.on('request', (req) => {
      const url = req.url();
      if (/^(data|blob|about):/.test(url)) return;
      if (new URL(url).origin !== origin) external.push(url);
      if (url.includes('/vendor/genui/')) vendor.push(url);
    });
    page.on('console', (msg) => { if (/Content Security Policy|securitypolicyviolation|CSPV/i.test(msg.text())) violations.push(msg.text()); });
    // Runs in every frame; only the opaque-origin sandbox iframe is asserted (host chunks probe eval on their own).
    await page.addInitScript(() => {
      if (window.parent === window) return;
      document.addEventListener('securitypolicyviolation', (e) => console.error(`CSPV ${e.violatedDirective} ${e.blockedURI} ${e.sourceFile}:${e.lineNumber} ${e.sample}`));
    });
    await mockChat(page, { assistantUi: D3_UI });
    await openChat(page);
    const frame = page.frameLocator('[data-testid="sandboxed-ui-iframe"]');
    await expect(frame.locator('#status')).toHaveText('d3 ok function');
    await expect(frame.locator('#chart svg')).toBeVisible();
    expect(vendor.some((u) => u.endsWith('/vendor/genui/d3.mjs'))).toBe(true);
    expect(external, `external requests: ${external.join(', ')}`).toEqual([]);
    expect(violations, `CSP violations: ${violations.join(' | ')}`).toEqual([]);
  });
});

test.describe('vi-vi locale and dark theme', () => {
  test.use({ locale: 'vi-VN', colorScheme: 'dark' });

  test('sandbox widget renders', async ({ page }) => {
    await mockChat(page, { assistantUi: SANDBOX_UI });
    await openChat(page);
    await expect(page.locator('html')).toHaveAttribute('lang', 'vi-VN');
    await expect.poll(() => page.evaluate(() => document.documentElement.classList.contains('dark'))).toBe(true);
    await expect(page.getByTestId('generative-ui-widget')).toBeVisible();
    await expect(page.frameLocator('[data-testid="sandboxed-ui-iframe"]').locator('#msg')).toHaveText('js ran');
    const bg = await page.getByTestId('sandboxed-ui-container').evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(bg).not.toBe('rgb(255, 255, 255)');
  });

  test('table widget renders', async ({ page }) => {
    await mockChat(page, { assistantUi: TABLE_UI });
    await openChat(page);
    await expect(page.getByTestId('generative-ui-widget')).toContainText('Quarterly revenue');
  });
});

test.describe('vendored library headers', () => {
  test('d3.mjs is served with CORS, CORP and an immutable cache', async ({ page }) => {
    const res = await page.request.get('/vendor/genui/d3.mjs');
    expect(res.status()).toBe(200);
    const headers = res.headers();
    expect(headers['access-control-allow-origin']).toBe('*');
    expect(headers['cross-origin-resource-policy']).toBe('cross-origin');
    expect(headers['cache-control']).toContain('immutable');
    expect(headers['cache-control']).toContain('max-age=31536000');
    expect(headers['content-type']).toMatch(/javascript/);
  });
});
