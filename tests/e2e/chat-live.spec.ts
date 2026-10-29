import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';

// Chat against the REAL stack (no page.route): API + chat worker + the fake OpenAI-compatible model
// (tests/integration/fake_model.py). Markers in the prompt steer the fake model: `[think]`, `[think-tag]`,
// `[fake:ui=table|sandbox]`, `[fake:tokens=N,delay_ms=M,think_delay_ms=T]`. Answers are "tok0 tok1 ...".

const SHOTS = 'C:/Users/doana/AppData/Local/Temp/claude/d--Project-Umwelt-OS/21938e12-9484-411c-8608-a17989a35b30/scratchpad/chat-shots';
const BASE_URL = 'http://fake-model:8000/v1';
const REASONING_TEXT = 'Considering the question';
let probed = false;
const ANSWER = /tok0 tok1 tok2 tok3 tok4/;

/** Owner write headers: Playwright's request context sends no Origin, which owner writes require. */
async function writeHeaders(page: Page) {
  const session = await (await page.request.get('/api/v1/auth/session')).json();
  return { 'X-CSRF-Token': session.csrfToken as string, Origin: new URL(page.url() === 'about:blank' ? 'http://localhost:3000' : page.url()).origin };
}

/** Points the owner's chat role at the fake model through the real settings API (idempotent) and probes it. */
async function configureFakeModel(page: Page) {
  await page.goto('/');
  const headers = await writeHeaders(page);
  const get = async () => (await page.request.get('/api/v1/settings/ai')).json();
  const current = await get();
  const fakeReady = current.defaults?.chat?.model === 'fake-chat' && current.providers?.some((p: { id: string; base_url?: string }) => p.id === 'default' && p.base_url === BASE_URL);
  if (!fakeReady) {
    const common = {
      defaults: { chat: { model: 'fake-chat', destination: 'remote', provider_id: 'default' }, brief: { model: 'fake-chat', destination: 'remote', provider_id: 'default' } },
      request_timeout_seconds: 30,
    };
    const created = await page.request.put('/api/v1/settings/ai', { headers, data: {
      ...common,
      providers: [{ id: 'default', kind: 'custom', label: 'fake', base_url: BASE_URL, credential_action: 'replaced', api_key: 'fake-model-key', models: [{ id: 'fake-chat', type: 'llm' }] }],
      expected_revision: current.configuration_revision,
    } });
    expect(created.ok(), `create provider: ${created.status()} ${await created.text()}`).toBeTruthy();
    const body = await created.json();
    const destination = body.providers.find((p: { id: string }) => p.id === 'default').destination_id;
    const saved = await page.request.put('/api/v1/settings/ai', { headers, data: {
      ...common,
      privacy: { allow_remote_reasoning: true, reasoning_destinations: [destination], allow_remote_embeddings: false, embedding_destinations: [] },
      expected_revision: body.configuration_revision,
    } });
    expect(saved.ok(), `grant consent: ${saved.status()} ${await saved.text()}`).toBeTruthy();
  }
  if (probed) return; // the probe route has a small request budget (429 model_probe); once per worker is enough
  probed = true;
  for (const capability of ['streaming', 'tools']) {
    const probe = await page.request.post('/api/v1/settings/models/chat/test', { headers, data: { capability } });
    expect(probe.ok(), `probe ${capability}: ${probe.status()} ${await probe.text()}`).toBeTruthy();
    expect((await probe.json()).result).toBe('supported');
  }
}

/**
 * Removes the fake provider again: system.spec.ts (runs later, alphabetically) asserts a fresh stack has no
 * configured model gateway, so this spec must not leave one behind.
 */
test.afterAll(async ({ browser }, testInfo) => {
  const context = await browser.newContext({ storageState: testInfo.project.use.storageState as string, baseURL: testInfo.project.use.baseURL });
  const page = await context.newPage();
  await page.goto('/');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const res = await page.request.put('/api/v1/settings/ai', { headers, data: { providers: [], defaults: {}, privacy: { allow_remote_reasoning: false, reasoning_destinations: [], allow_remote_embeddings: false, embedding_destinations: [] }, request_timeout_seconds: 30, expected_revision: current.configuration_revision } });
  expect(res.ok(), `remove fake provider: ${res.status()} ${await res.text()}`).toBeTruthy();
  await context.close();
});

// The textarea that shares a composer container with the visible Send button (other textareas may be mounted but hidden).
const composer = (page: Page) => page.locator('[data-testid="chat-send"]:visible').first().locator('xpath=ancestor::div[.//textarea][1]//textarea').first();

/** Types a prompt, sends it and returns the conversation id the real API created for it. */
async function send(page: Page, prompt: string): Promise<string> {
  // Re-fill until Send enables: a fill that lands before hydration/settings load is discarded by React.
  await expect(async () => {
    await composer(page).fill(prompt);
    await expect(page.locator('[data-testid="chat-send"]:visible')).toHaveAttribute('aria-disabled', 'false', { timeout: 2000 });
  }).toPass({ timeout: 20_000 });
  const sent = page.waitForResponse((r) => /\/api\/v1\/conversations\/[^/]+\/messages$/.test(r.url()) && r.request().method() === 'POST');
  await page.locator('[data-testid="chat-send"]:visible').click();
  const response = await sent;
  expect(response.status()).toBe(202);
  return response.url().match(/conversations\/([^/]+)\/messages/)![1];
}

/** Gives the conversation a unique title (real PATCH) so the history rail can reopen it after a reload. */
async function titled(page: Page, id: string, title: string) {
  const res = await page.request.patch(`/api/v1/conversations/${id}`, { headers: await writeHeaders(page), data: { title } });
  expect(res.ok(), `rename: ${res.status()}`).toBeTruthy();
}

/** Opens a saved conversation from the history rail (collapsed by default). */
async function openConversation(page: Page, title: string) {
  const toggle = page.locator('[aria-controls="chat-history-sidebar"]').first();
  await toggle.waitFor();
  if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click();
  await page.getByText(title).first().click();
}

/** Waits until the run is over (stop button gone) so the transcript is the persisted one. */
async function settled(page: Page) {
  await expect(page.getByTestId('chat-stop')).toHaveCount(0, { timeout: 60_000 });
}

test.describe('chat (real stack, fake model)', () => {
  test.beforeEach(async ({ page }) => { await configureFakeModel(page); });

  test('reasoning streams (thinking then done), answer renders and both persist after reload', async ({ page }, testInfo) => {
    const title = `e2e-reasoning-${testInfo.workerIndex}-${Date.now()}`;
    await page.goto('/chat');
    const id = await send(page, '[think] [fake:think_delay_ms=800] explain');
    const block = page.getByTestId('chat-reasoning');
    await expect(block).toHaveAttribute('data-state', 'thinking');
    await expect(block).toContainText(REASONING_TEXT);
    await expect(page.getByText(ANSWER)).toBeVisible();
    await expect(block).toHaveAttribute('data-state', 'done');
    await expect(block).not.toHaveAttribute('open', '');
    await settled(page);
    await titled(page, id, title);

    await page.reload();
    await openConversation(page, title);
    const persisted = page.getByTestId('chat-reasoning');
    await expect(persisted).toBeVisible();
    await expect(persisted).toHaveAttribute('data-state', 'done');
    await expect(persisted).not.toHaveAttribute('open', '');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await persisted.locator('summary').click();
    await expect(persisted).toHaveAttribute('open', '');
    await expect(persisted).toContainText('Considering the question… checking sources… drafting an answer.');
    await page.screenshot({ path: `${SHOTS}/desktop-light-en-reasoning-expanded.png` });
  });

  test('inline <think> tags become reasoning, never literal answer text', async ({ page }) => {
    await page.goto('/chat');
    await send(page, '[think-tag] hello');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await settled(page);
    const block = page.getByTestId('chat-reasoning');
    await expect(block).toContainText(REASONING_TEXT);
    await expect(page.locator('main')).not.toContainText('<think>');
    await expect(page.locator('main')).not.toContainText('</think>');
    await expect(page.locator('main')).not.toContainText('<thi');
  });

  test('generative UI: table and sandboxed widgets render and persist after reload', async ({ page }, testInfo) => {
    await page.goto('/chat');
    const tableId = await send(page, 'show a table [fake:ui=table,delay_ms=60]');
    // The fake model answers the UI tool call once per turn, so one answer carries exactly one widget.
    const table = page.getByTestId('generative-ui-widget').first();
    await expect(table).toContainText('Fake quarterly revenue', { timeout: 30_000 });
    await expect(table).toContainText('Q2');
    await settled(page);
    await expect(page.getByTestId('generative-ui-widget')).toHaveCount(1);
    const tableTitle = `e2e-table-${testInfo.workerIndex}-${Date.now()}`;
    await titled(page, tableId, tableTitle);
    await page.reload();
    await openConversation(page, tableTitle);
    await expect(page.getByTestId('generative-ui-widget').first()).toContainText('Q1');
    await page.screenshot({ path: `${SHOTS}/desktop-light-en-widget-table.png` });

    await page.getByRole('button', { name: 'New chat' }).first().click();
    const sandboxId = await send(page, 'show a sandbox [fake:ui=sandbox,delay_ms=30]');
    const container = page.getByTestId('sandboxed-ui-container').first();
    await expect(container).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('sandboxed-ui-badge').first()).toBeVisible();
    await expect(page.getByTestId('sandboxed-ui-iframe').first()).toBeVisible();
    await expect(page.frameLocator('[data-testid="sandboxed-ui-iframe"]').first().locator('#msg')).toHaveText('fake js ran', { timeout: 30_000 });
    await settled(page);
    const sandboxTitle = `e2e-sandbox-${testInfo.workerIndex}-${Date.now()}`;
    await titled(page, sandboxId, sandboxTitle);
    await page.reload();
    await openConversation(page, sandboxTitle);
    await expect(page.getByTestId('sandboxed-ui-container').first()).toBeVisible();
    await expect(page.frameLocator('[data-testid="sandboxed-ui-iframe"]').first().locator('#msg')).toHaveText('fake js ran', { timeout: 30_000 });
    await page.screenshot({ path: `${SHOTS}/desktop-light-en-widget-sandbox.png` });
  });

  test('Stop mid-stream is a neutral cancellation and the composer stays usable', async ({ page }) => {
    await page.goto('/chat');
    await send(page, 'long answer [fake:tokens=300,delay_ms=100]');
    await expect(page.getByText(/tok2 /)).toBeVisible({ timeout: 30_000 });
    await page.getByTestId('chat-stop').click();
    await expect(page.getByTestId('chat-cancelled')).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: /\S/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0);
    await expect(page.getByTestId('chat-stop')).toHaveCount(0);
    // Composer is usable: a follow-up turn streams and finishes.
    await send(page, 'again');
    await expect(page.getByText(ANSWER).last()).toBeVisible();
  });

  test('Regenerate answer appends a new answer on the same model', async ({ page }) => {
    await page.goto('/chat');
    const id = await send(page, 'regen me');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await settled(page);
    const assistants = async () => {
      const detail = await (await page.request.get(`/api/v1/conversations/${id}`)).json();
      return detail.messages.filter((m: { role: string }) => m.role === 'assistant') as Array<{ id: string; model_identity: string | null }>;
    };
    await expect.poll(async () => (await assistants()).length).toBe(1);
    const first = (await assistants())[0];
    const mutation = page.waitForRequest((r) => /\/messages\/[^/]+\/mutations$/.test(r.url()) && r.method() === 'POST');
    await page.getByRole('button', { name: 'Regenerate answer' }).last().click();
    const body = (await mutation).postDataJSON() as Record<string, unknown>;
    expect(body.action).toBe('regenerate');
    // Either no explicit choice (server default) or exactly the configured fake model.
    if (body.model !== undefined) expect(body.model).toBe('fake-chat');
    await expect.poll(async () => (await assistants()).length, { timeout: 30_000 }).toBe(2);
    const second = (await assistants()).find((m) => m.id !== first.id)!;
    expect(second.model_identity).toBe(first.model_identity);
    await expect(page.getByText(ANSWER).last()).toBeVisible();
  });

  test('Rename and Delete conversation work from the header with history collapsed', async ({ page }, testInfo) => {
    await page.goto('/chat');
    const id = await send(page, 'rename and delete me');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await settled(page);
    const history = page.locator('[aria-controls="chat-history-sidebar"]').first();
    await expect(history).toHaveAttribute('aria-expanded', 'false');

    const renamed = `e2e-renamed-${testInfo.workerIndex}-${Date.now()}`;
    await page.getByRole('button', { name: 'Rename conversation' }).click();
    const input = page.getByRole('dialog').getByLabel(/./).first();
    await input.fill(renamed);
    await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('heading', { level: 1, name: renamed })).toBeVisible();
    expect((await (await page.request.get(`/api/v1/conversations/${id}`)).json()).title).toBe(renamed);

    await page.getByRole('button', { name: 'Delete conversation' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Delete conversation' }).click();
    await expect(page.getByRole('heading', { level: 1, name: renamed })).toHaveCount(0);
    await expect.poll(async () => (await page.request.get(`/api/v1/conversations/${id}`)).status()).toBe(404);
  });

  test('a send the server rejects (422, over-long prompt) keeps the draft', async ({ page }) => {
    await page.goto('/chat');
    const tooLong = 'x'.repeat(20_001); // schema caps content at 20 000 characters
    await composer(page).fill(tooLong);
    const rejected = page.waitForResponse((r) => /\/messages$/.test(r.url()) && r.request().method() === 'POST');
    await page.getByTestId('chat-send').click();
    expect((await rejected).status()).toBe(422);
    await expect(composer(page)).toHaveValue(tooLong);
    await expect(page.getByTestId('chat-stop')).toHaveCount(0);
  });
});

test.describe('chat vi-vi + dark', () => {
  test.use({ locale: 'vi-VN', colorScheme: 'dark' });
  test('reasoning labels are Vietnamese and the dark theme renders', async ({ page }) => {
    await configureFakeModel(page);
    await page.goto('/chat');
    await expect(page.getByRole('button', { name: 'Cuộc trò chuyện mới' }).first()).toBeVisible();
    await send(page, '[think] [fake:think_delay_ms=800] giải thích');
    const block = page.getByTestId('chat-reasoning');
    await expect(block).toContainText('Đang suy nghĩ…');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await expect(block).toContainText('Quá trình suy nghĩ');
    await settled(page);
    await expect(page.locator('html')).toHaveClass(/dark/);
    await block.locator('summary').click();
    await page.screenshot({ path: `${SHOTS}/desktop-dark-vi-reasoning.png` });
  });
});

test.describe('chat mobile 375x812', () => {
  test.use({ viewport: { width: 375, height: 812 } });
  const noOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

  test('header More actions menu, no horizontal overflow, drawer composer usable', async ({ page }) => {
    await configureFakeModel(page);
    await page.goto('/chat');
    // First visit on a narrow viewport shows a modal "Better on a larger screen" notice.
    const gotIt = page.getByRole('button', { name: 'Got it' });
    await gotIt.waitFor({ state: 'visible', timeout: 8000 }).then(() => gotIt.click()).catch(() => undefined);
    await send(page, '[think] mobile check');
    await expect(page.getByText(ANSWER)).toBeVisible();
    await settled(page);
    await page.getByRole('button', { name: 'More actions' }).click();
    await expect(page.getByRole('menuitem', { name: 'Rename conversation' })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Delete conversation' })).toBeVisible();
    await page.keyboard.press('Escape');
    expect(await noOverflow(page)).toBe(true);
    // Bottom nav: every tab fully inside the 375px viewport, Settings reachable.
    const navLinks = page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link');
    await expect(navLinks.getByText('Settings')).toBeVisible();
    for (const link of await navLinks.all()) {
      const box = await link.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(375);
    }
    await page.screenshot({ path: `${SHOTS}/mobile-375x812-chat.png` });

    // Quick-chat drawer (opened from the command palette, available on any page).
    await page.goto('/');
    await page.getByRole('button', { name: 'Search or run a command' }).click();
    await page.getByText('Quick chat', { exact: true }).first().click();
    const drawer = page.getByRole('dialog');
    await expect(drawer).toBeVisible();
    await drawer.locator('textarea').fill('hi from the drawer');
    await drawer.getByTestId('chat-send').click();
    await expect(drawer.getByText(ANSWER)).toBeVisible();
    expect(await noOverflow(page)).toBe(true);
    await page.screenshot({ path: `${SHOTS}/mobile-375x812-drawer.png` });
  });
});
