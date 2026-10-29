import { existsSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';

// Opt-in chat e2e against a REAL OpenAI-compatible model. Configure `.env.e2e-real` (see `.env.e2e-real.example`);
// the whole file is skipped while the key, base URL or model is empty, so regular e2e groups are unaffected.
// SECURITY: the key goes only to the disposable stack's settings API. Traces are off (they would record the request
// body), nothing logs the key, and screenshots are taken on /chat only (never on the settings page).

// Resolve from this spec, not the cwd: `npm run test:e2e` runs Playwright from apps/web.
const ENV_FILE = path.resolve(__dirname, '../../.env.e2e-real');
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);

const KEY = process.env.E2E_REAL_API_KEY ?? '';
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? '').replace(/\/+$/, '');
const MODEL = process.env.E2E_REAL_MODEL ?? '';
const EXPECT_REASONING = process.env.E2E_REAL_EXPECT_REASONING === 'true';
// Optional low|medium|high: stored on the chat role so request-gated models stream reasoning.
const EFFORT = ['low', 'medium', 'high'].includes(process.env.E2E_REAL_REASONING_EFFORT ?? '') ? process.env.E2E_REAL_REASONING_EFFORT : undefined;
const EXPECT_TOOLS = process.env.E2E_REAL_EXPECT_TOOLS === 'true';
const SHOTS = 'C:/Users/doana/AppData/Local/Temp/claude/d--Project-Umwelt-OS/21938e12-9484-411c-8608-a17989a35b30/scratchpad/chat-shots';
const REAL_TIMEOUT = 90_000;

test.use({ trace: 'off', screenshot: 'off', video: 'off' });

/** Redacts the configured key from any text that may reach an assertion message or report. */
const mask = (text: string) => (KEY ? text.split(KEY).join('***') : text);

/** Owner write headers: Playwright's request context sends no Origin, which owner writes require. */
async function writeHeaders(page: Page) {
  const session = await (await page.request.get('/api/v1/auth/session')).json();
  return { 'X-CSRF-Token': session.csrfToken as string, Origin: new URL(page.url() === 'about:blank' ? 'http://localhost:3000' : page.url()).origin };
}

/** Fails if the key appears anywhere in the rendered page (run after every test). */
async function expectNoKeyLeak(page: Page) {
  expect((await page.content()).includes(KEY), 'API key must never be rendered').toBe(false);
}

/** Configures the real provider through the settings API (key stays in the request body only) and probes it. */
async function configureRealModel(page: Page) {
  await page.goto('/');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const common = {
    defaults: { chat: { model: MODEL, destination: 'remote', provider_id: 'real', ...(EFFORT ? { reasoning_effort: EFFORT } : {}) }, brief: { model: MODEL, destination: 'remote', provider_id: 'real' } },
    request_timeout_seconds: 120,
  };
  const created = await page.request.put('/api/v1/settings/ai', { headers, data: {
    ...common,
    providers: [{ id: 'real', kind: 'custom', label: 'real', base_url: BASE_URL, credential_action: 'replaced', api_key: KEY, models: [{ id: MODEL, type: 'llm' }] }],
    expected_revision: current.configuration_revision,
  } });
  expect(created.ok(), mask(`create provider: ${created.status()} ${await created.text()}`)).toBeTruthy();
  const body = await created.json();
  const destination = body.providers.find((p: { id: string }) => p.id === 'real').destination_id;
  const saved = await page.request.put('/api/v1/settings/ai', { headers, data: {
    ...common,
    privacy: { allow_remote_reasoning: true, reasoning_destinations: [destination], allow_remote_embeddings: false, embedding_destinations: [] },
    expected_revision: body.configuration_revision,
  } });
  expect(saved.ok(), mask(`grant consent: ${saved.status()} ${await saved.text()}`)).toBeTruthy();
  const capabilities = EXPECT_TOOLS ? ['streaming', 'tools'] : ['streaming'];
  for (const capability of capabilities) {
    const probe = await page.request.post('/api/v1/settings/models/chat/test', { headers, data: { capability }, timeout: REAL_TIMEOUT });
    expect(probe.ok(), mask(`probe ${capability}: ${probe.status()} ${await probe.text()}`)).toBeTruthy();
    expect((await probe.json()).result, `probe ${capability}`).toBe('supported');
  }
}

/** Removes the provider: system.spec.ts asserts a fresh stack has no configured model gateway. */
async function removeProvider(page: Page) {
  await page.goto('/');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const res = await page.request.put('/api/v1/settings/ai', { headers, data: { providers: [], defaults: {}, privacy: { allow_remote_reasoning: false, reasoning_destinations: [], allow_remote_embeddings: false, embedding_destinations: [] }, request_timeout_seconds: 30, expected_revision: current.configuration_revision } });
  expect(res.ok(), mask(`remove provider: ${res.status()}`)).toBeTruthy();
}

// The textarea that shares a composer container with the visible Send button.
const composer = (page: Page) => page.locator('[data-testid="chat-send"]:visible').first().locator('xpath=ancestor::div[.//textarea][1]//textarea').first();

/** Types a prompt, sends it and returns the conversation id the real API created. */
async function send(page: Page, prompt: string): Promise<string> {
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

/** Waits until the run is over (stop button gone). */
async function settled(page: Page) {
  await expect(page.getByTestId('chat-stop')).toHaveCount(0, { timeout: REAL_TIMEOUT });
}

/** Assistant messages persisted for a conversation. */
async function assistants(page: Page, id: string) {
  const detail = await (await page.request.get(`/api/v1/conversations/${id}`)).json();
  return detail.messages.filter((m: { role: string }) => m.role === 'assistant') as Array<{ id: string; content: string }>;
}

test.describe('chat (real model)', () => {
  test.skip(!KEY || !BASE_URL || !MODEL, 'set E2E_REAL_API_KEY / E2E_REAL_BASE_URL / E2E_REAL_MODEL in .env.e2e-real');
  test.setTimeout(3 * REAL_TIMEOUT);

  test.beforeEach(async ({ page }) => { await configureRealModel(page); });
  test.afterEach(async ({ page }) => { await expectNoKeyLeak(page); });
  test.afterAll(async ({ browser }, testInfo) => {
    const context = await browser.newContext({ storageState: testInfo.project.use.storageState as string, baseURL: testInfo.project.use.baseURL });
    await removeProvider(await context.newPage());
    await context.close();
  });

  test('simple prompt streams, settles without error and persists after reload', async ({ page }, testInfo) => {
    const title = `e2e-real-${testInfo.workerIndex}-${Date.now()}`;
    await page.goto('/chat');
    const id = await send(page, 'Reply with one short sentence about the sea.');
    await settled(page);
    await expect.poll(async () => (await assistants(page, id)).at(-1)?.content.trim().length ?? 0, { timeout: REAL_TIMEOUT }).toBeGreaterThan(0);
    await expect(page.getByRole('alert').filter({ hasText: /\S/ })).toHaveCount(0);
    const answer = (await assistants(page, id)).at(-1)!.content.trim();
    const renamed = await page.request.patch(`/api/v1/conversations/${id}`, { headers: await writeHeaders(page), data: { title } });
    expect(renamed.ok()).toBeTruthy();

    // Reopen from the history rail (collapsed by default) after a reload.
    await page.reload();
    const toggle = page.locator('[aria-controls="chat-history-sidebar"]').first();
    await toggle.waitFor();
    if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click();
    await page.getByText(title).first().click();
    await expect(page.getByText(answer.slice(0, 40), { exact: false }).first()).toBeVisible({ timeout: 30_000 });
  });

  test('reasoning block streams then completes; no literal <think>', async ({ page }, testInfo) => {
    test.skip(!EXPECT_REASONING, 'E2E_REAL_EXPECT_REASONING is not true');
    await page.goto('/chat');
    const id = await send(page, 'A bat and a ball cost $1.10; the bat costs $1 more than the ball. Think step by step: how much is the ball?');
    const block = page.getByTestId('chat-reasoning');
    await settled(page);
    // Record what the server persisted before asserting the UI, so a failure says whether the model or the UI dropped it.
    const last = (await assistants(page, id)).at(-1) as { reasoning?: string | null } | undefined;
    testInfo.annotations.push({ type: 'persisted-reasoning-chars', description: String(last?.reasoning?.length ?? 0) });
    console.log(`persisted-reasoning-chars=${last?.reasoning?.length ?? 0}`);
    // The app contract is "persisted reasoning is shown"; a real model may still decide a turn needs no thinking.
    test.skip(!last?.reasoning, 'the model streamed no reasoning for this turn (model choice, not an app failure)');
    await expect(block).toBeVisible({ timeout: REAL_TIMEOUT });
    await expect(block).toHaveAttribute('data-state', 'done');
    await block.locator('summary').click();
    expect(((await block.textContent()) ?? '').trim().length).toBeGreaterThan(0);
    expect((await assistants(page, id)).at(-1)!.content).not.toContain('<think>');
    await expect(page.locator('body')).not.toContainText('<think>');
    await page.screenshot({ path: `${SHOTS}/real-desktop-light-en-reasoning-expanded.png` });
  });

  test('table prompt renders a generative or sandboxed widget', async ({ page }) => {
    test.skip(!EXPECT_TOOLS, 'E2E_REAL_EXPECT_TOOLS is not true');
    await page.goto('/chat');
    await send(page, 'Show a table of the 3 largest planets with diameter. Use the UI tool to render it as a table.');
    // The model may pick either renderer.
    const widget = page.getByTestId('generative-ui-widget').or(page.getByTestId('sandboxed-ui-container')).first();
    await expect(widget).toBeVisible({ timeout: REAL_TIMEOUT });
    await settled(page);
    await page.screenshot({ path: `${SHOTS}/real-desktop-light-en-widget.png` });
  });

  test('Stop mid-stream is a neutral cancellation', async ({ page }) => {
    await page.goto('/chat');
    await send(page, 'Write a very long, detailed essay (at least 1500 words) about the history of the printing press.');
    await expect(page.getByTestId('chat-stop')).toBeVisible({ timeout: REAL_TIMEOUT });
    // Let the model start emitting so the stop lands mid-stream rather than before the first token.
    await page.waitForTimeout(4000);
    await page.getByTestId('chat-stop').click();
    await expect(page.getByTestId('chat-cancelled')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('chat-stop')).toHaveCount(0);
  });

  test('Regenerate appends a second answer', async ({ page }) => {
    await page.goto('/chat');
    const id = await send(page, 'Name one primary colour in a single word.');
    await settled(page);
    await expect.poll(async () => (await assistants(page, id)).length, { timeout: REAL_TIMEOUT }).toBe(1);
    await page.getByRole('button', { name: 'Regenerate answer' }).last().click();
    await expect.poll(async () => (await assistants(page, id)).length, { timeout: REAL_TIMEOUT }).toBe(2);
    await settled(page);
    expect((await assistants(page, id))[1].content.trim().length).toBeGreaterThan(0);
  });

  // ponytail: wrong-key negative test skipped; it needs a second provider and the real endpoint's 401 shape.
});
