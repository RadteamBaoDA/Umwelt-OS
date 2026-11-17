import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { BUILTINS, collectedRecordCount, enableBuiltinViaSettings, writeHeaders } from './sources-helpers';

// TEMPORARY design-review capture for docs/audit/2026-11-11-chat-uiux-review.html (delete after the review).
// Seeds two keyless built-in sources, configures the real model from .env.e2e-real and screenshots Chat states.
// SECURITY: same rules as chat-real-model.spec.ts: traces off, key only in the settings API body, no settings-page shots.

const ENV_FILE = path.resolve(__dirname, '../../.env.e2e-real');
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);
const KEY = process.env.E2E_REAL_API_KEY ?? '';
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? '').replace(/\/+$/, '');
const MODEL = process.env.E2E_REAL_MODEL ?? '';
const OUT = process.env.CHAT_REVIEW_DIR ?? path.resolve(__dirname, '../../test-results/chat-review');
const REAL_TIMEOUT = 120_000;

test.use({ trace: 'off', screenshot: 'off', video: 'off' });
test.skip(!KEY || !BASE_URL || !MODEL, 'needs .env.e2e-real');

const composer = (page: Page) => page.locator('[data-testid="chat-send"]:visible').first().locator('xpath=ancestor::div[.//textarea][1]//textarea').first();

async function shot(page: Page, name: string) {
  mkdirSync(OUT, { recursive: true });
  await page.screenshot({ path: `${OUT}/${name}.png`, animations: 'disabled' }).catch((e) => console.log(`SHOT ${name} failed: ${String(e).split('\n')[0]}`));
}

async function send(page: Page, prompt: string) {
  await expect(async () => {
    await composer(page).fill(prompt);
    await expect(page.locator('[data-testid="chat-send"]:visible')).toHaveAttribute('aria-disabled', 'false', { timeout: 2000 });
  }).toPass({ timeout: 20_000 });
  const sent = page.waitForResponse((r) => /\/api\/v1\/conversations\/[^/]+\/messages$/.test(r.url()) && r.request().method() === 'POST');
  await page.locator('[data-testid="chat-send"]:visible').click();
  await sent;
}

const settled = (page: Page) => expect(page.getByTestId('chat-stop')).toHaveCount(0, { timeout: REAL_TIMEOUT });

/** Three framings of the current page: desktop light, desktop dark, phone light. */
async function shotAll(page: Page, name: string) {
  await shot(page, `${name}-1440-light`);
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.waitForTimeout(400);
  await shot(page, `${name}-1440-dark`);
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(600);
  await shot(page, `${name}-390-light`);
  await page.setViewportSize({ width: 1440, height: 900 });
}

test.beforeEach(async ({ page }) => { await page.setViewportSize({ width: 1440, height: 900 }); });

test('seed data and configure the real model', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  for (const id of ['thanhnien_news', 'emsc_quakes']) {
    try {
      const out = await enableBuiltinViaSettings(page, id, BUILTINS.find(([key]) => key === id)![1], 90_000);
      console.log(`SEED ${id}: ok=${out.ok} records=${out.sourceId ? await collectedRecordCount(page, out.sourceId, 60_000) : 0}`);
    } catch (error) { console.log(`SEED ${id} skipped: ${String(error).split('\n')[0]}`); }
  }
  await page.goto('/dashboard');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const common = { defaults: { chat: { model: MODEL, destination: 'remote', provider_id: 'real', reasoning_effort: 'low' }, brief: { model: MODEL, destination: 'remote', provider_id: 'real' } }, request_timeout_seconds: 120 };
  const created = await page.request.put('/api/v1/settings/ai', { headers, data: { ...common, providers: [{ id: 'real', kind: 'custom', label: 'real', base_url: BASE_URL, credential_action: 'replaced', api_key: KEY, models: [{ id: MODEL, type: 'llm' }] }], expected_revision: current.configuration_revision } });
  expect(created.ok(), `create provider: ${created.status()}`).toBeTruthy();
  const body = await created.json();
  const destination = body.providers.find((p: { id: string }) => p.id === 'real').destination_id;
  const saved = await page.request.put('/api/v1/settings/ai', { headers, data: { ...common, privacy: { allow_remote_reasoning: true, reasoning_destinations: [destination], allow_remote_embeddings: false, embedding_destinations: [] }, expected_revision: body.configuration_revision } });
  expect(saved.ok(), `grant consent: ${saved.status()}`).toBeTruthy();
});

test('empty state and composer menu', async ({ page }) => {
  await page.goto('/chat');
  await composer(page).waitFor();
  await page.waitForTimeout(1500);
  await shotAll(page, '01-empty');
  await page.locator('#chat-composer-menu').click();
  await page.waitForTimeout(400);
  await shot(page, '02-composer-menu-1440-light');
});

test('streaming, scroll follow (C4) and stop (C2)', async ({ page }) => {
  test.setTimeout(4 * 60_000);
  await page.goto('/chat');
  await send(page, 'Write a very long, detailed essay (at least 1500 words) about the history of the printing press, with headings.');
  await expect(page.getByTestId('chat-stop')).toBeVisible({ timeout: REAL_TIMEOUT });
  await shot(page, '03-pending-1440-light');
  await page.waitForTimeout(9000);
  const m = await page.locator('[role="log"]').first().evaluate((el) => ({ top: Math.round(el.scrollTop), height: el.scrollHeight, client: el.clientHeight }));
  console.log(`C4 mid-stream scrollTop=${m.top} scrollHeight=${m.height} clientHeight=${m.client} hiddenBelow=${m.height - m.top - m.client}px`);
  await shot(page, '04-streaming-1440-light');
  await page.getByTestId('chat-stop').click();
  await expect(page.getByTestId('chat-cancelled')).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  const bubble = page.locator('[data-streaming-answer]');
  console.log(`C2 after-stop streamingBubble=${await bubble.count()} spinner=${await bubble.locator('.animate-spin').count()} statusText="${(await bubble.locator('[role="status"]').first().textContent().catch(() => '')) ?? ''}"`);
  await page.locator('[role="log"]').first().evaluate((el) => el.scrollTo(0, el.scrollHeight));
  await shot(page, '05-stopped-1440-light');
  await page.waitForTimeout(4000);
  console.log(`C2 after-refetch streamingBubble=${await bubble.count()} assistantMessages=${await page.locator('[data-role="assistant"]').count()}`);
  await shot(page, '05b-stopped-after-refetch-1440-light');
});

test('settled rich answer, message actions, edit mode', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await page.goto('/chat');
  await send(page, 'Explain HTTP caching briefly. Use an H2 heading, an H3 heading, a bullet list, a small markdown table and one short code block.');
  await settled(page);
  await page.waitForTimeout(3000);
  await shotAll(page, '06-answer-markdown');
  await page.locator('[role="log"]').first().evaluate((el) => el.scrollTo(0, 0));
  await shot(page, '07-answer-top-1440-light');
  await page.getByRole('button', { name: 'Edit prompt' }).first().click();
  await page.waitForTimeout(500);
  await shot(page, '08-edit-mode-1440-light');
});

test('grounded answer with sources rail', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await page.goto('/chat');
  await send(page, 'Using my collected data, what are the most recent earthquakes and the latest Thanh Nien news headlines? Cite your sources.');
  await settled(page);
  await page.waitForTimeout(3000);
  await shotAll(page, '09-grounded');
  const strip = page.getByTestId('sources-strip').first();
  console.log(`SOURCES strip=${await strip.count()} cites=${await page.locator('a.cite').count()} rail=${await page.locator('#chat-evidence-rail').count()}`);
  if (await strip.count()) { await strip.click(); await page.waitForTimeout(600); await shot(page, '10-sources-rail-1440-light'); }
});

test('history sidebar, row menu, header menu, quick chat drawer', async ({ page }) => {
  await page.goto('/chat');
  const toggle = page.locator('[aria-controls="chat-history-sidebar"]').first();
  await toggle.waitFor();
  if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click();
  await page.waitForTimeout(1500);
  const row = page.locator('#chat-history-sidebar [aria-current], #chat-history-sidebar section button').first();
  await row.click().catch(() => undefined);
  await page.waitForTimeout(2000);
  await shotAll(page, '11-history');
  await page.getByRole('button', { name: 'Conversation actions' }).first().click({ force: true }).catch(() => undefined);
  await page.waitForTimeout(400);
  await shot(page, '12-history-row-menu-1440-light');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'More actions' }).first().click().catch(() => undefined);
  await page.waitForTimeout(400);
  await shot(page, '13-header-more-menu-1440-light');
  await page.keyboard.press('Escape');
  await page.goto('/dashboard');
  await page.waitForTimeout(2500);
  await page.keyboard.press('Control+j');
  await page.getByRole('dialog').waitFor({ timeout: 15_000 }).catch(() => undefined);
  await page.waitForTimeout(2000);
  await shotAll(page, '14-quick-chat');
});

test('cleanup: remove the real provider', async ({ page }) => {
  await page.goto('/dashboard');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const res = await page.request.put('/api/v1/settings/ai', { headers, data: { providers: [], defaults: {}, privacy: { allow_remote_reasoning: false, reasoning_destinations: [], allow_remote_embeddings: false, embedding_destinations: [] }, request_timeout_seconds: 30, expected_revision: current.configuration_revision } });
  expect(res.ok()).toBeTruthy();
});
