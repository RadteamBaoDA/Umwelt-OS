import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { writeHeaders } from './sources-helpers';

// Opt-in end-to-end run of "create a scheduled task from chat" (Phase 3) with a REAL OpenAI-compatible model.
// The real model must call `draft_scheduled_task`; the card saves nothing until the owner clicks Save in the editor.
// Configure `.env.e2e-real` (see `.env.e2e-real.example`); the file is skipped while the key, base URL or model is empty.
// SECURITY: the key goes only to the disposable stack's settings API, traces/videos are off, and screenshots are never
// taken on the Models settings page.

const ENV_FILE = path.resolve(__dirname, '../../.env.e2e-real');
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);

const KEY = process.env.E2E_REAL_API_KEY ?? '';
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? '').replace(/\/+$/, '');
const MODEL = process.env.E2E_REAL_MODEL ?? '';
const SHOTS = process.env.E2E_SHOTS_DIR ?? path.resolve(__dirname, '../../test-results/chat-task-shots');
const REAL_TIMEOUT = 180_000;

test.use({ trace: 'off', video: 'off', screenshot: 'off' });
test.describe.configure({ mode: 'serial' });
test.skip(!KEY || !BASE_URL || !MODEL, 'set E2E_REAL_BASE_URL, E2E_REAL_API_KEY and E2E_REAL_MODEL in .env.e2e-real');

/** Redacts the configured key from text that may reach an assertion message or the report. */
const mask = (text: string) => (KEY ? text.split(KEY).join('***') : text);

/** Saves a full-page screenshot for the HTML report (never on settings pages that show credentials). */
async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true, animations: 'disabled' });
}

type Automation = { id: string; revision: number; trigger: unknown; actions: { type: string }[] };
type Message = { role: string; content: string; task_draft?: { kind: string; time: string; days: string; sections?: string[] } | null };

/** Daily brief rules of the workspace (one schedule rule with a single generate_brief action). */
async function briefRules(page: Page): Promise<Automation[]> {
  const body = (await (await page.request.get('/api/v1/automations')).json()) as { items: Automation[] };
  return body.items.filter((rule) => rule.actions.length === 1 && rule.actions[0].type === 'generate_brief');
}

/** Deletes every Daily brief rule so the one-per-workspace slot is free (disposable stack only). */
async function clearBriefRules(page: Page) {
  const headers = await writeHeaders(page);
  for (const rule of await briefRules(page)) {
    const res = await page.request.delete(`/api/v1/automations/${rule.id}?expected_revision=${rule.revision}`, { headers });
    expect(res.ok(), `delete brief rule: ${res.status()}`).toBeTruthy();
  }
}

/** Points the chat role at the real provider and grants remote reasoning consent. */
async function configureRealModel(page: Page) {
  await page.goto('/dashboard');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const defaults = { chat: { model: MODEL, destination: 'remote', provider_id: 'real' }, brief: { model: MODEL, destination: 'remote', provider_id: 'real' } };
  const created = await page.request.put('/api/v1/settings/ai', { headers, data: {
    defaults, request_timeout_seconds: 120,
    providers: [{ id: 'real', kind: 'custom', label: 'real', base_url: BASE_URL, credential_action: 'replaced', api_key: KEY, models: [{ id: MODEL, type: 'llm' }] }],
    expected_revision: current.configuration_revision,
  } });
  expect(created.ok(), mask(`create provider: ${created.status()} ${await created.text()}`)).toBeTruthy();
  const body = await created.json();
  const destination = body.providers.find((p: { id: string }) => p.id === 'real').destination_id;
  const saved = await page.request.put('/api/v1/settings/ai', { headers, data: {
    defaults, request_timeout_seconds: 120,
    privacy: { allow_remote_reasoning: true, reasoning_destinations: [destination], allow_remote_embeddings: false, embedding_destinations: [] },
    expected_revision: body.configuration_revision,
  } });
  expect(saved.ok(), mask(`grant consent: ${saved.status()} ${await saved.text()}`)).toBeTruthy();
}

let conversationId = '';

test('asking in chat shows an unsaved Daily brief draft card from the real model', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await configureRealModel(page);
  await clearBriefRules(page);
  await page.goto('/chat');
  const composer = page.getByRole('textbox').last();
  await composer.fill(
    'Hãy tạo cho tôi một scheduled task Daily brief lúc 06:30 các ngày trong tuần (thứ Hai đến thứ Sáu), chỉ gồm tin tức và thời tiết.',
  );
  await composer.press('Enter');
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}/, { timeout: 60_000 });
  conversationId = /\/chat\/([0-9a-f-]{36})/.exec(page.url())![1];

  let draft: Message['task_draft'] = null;
  await expect.poll(async () => {
    const detail = (await (await page.request.get(`/api/v1/conversations/${conversationId}`)).json()) as { messages: Message[] };
    draft = detail.messages.find((m) => m.role === 'assistant' && m.task_draft)?.task_draft ?? null;
    return draft ? 'draft' : 'pending';
  }, { timeout: REAL_TIMEOUT, intervals: [3_000], message: 'real model proposes a task draft' }).toBe('draft');
  test.info().annotations.push({ type: 'draft', description: JSON.stringify(draft) });
  expect(draft!.kind).toBe('daily_brief');
  expect(draft!.time).toBe('06:30');
  expect(draft!.days).toBe('weekdays');

  const card = page.getByTestId('task-draft-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card.getByText('Not saved yet')).toBeVisible();
  await expect(card.getByText('Weekdays at', { exact: false })).toBeVisible();
  // Nothing is saved by the chat turn itself.
  expect(await briefRules(page)).toHaveLength(0);
  await shot(page, '01-chat-draft-card');
  expect((await page.content()).includes(KEY), 'API key never rendered').toBe(false);
});

test('Review and create opens the pre-filled editor; Save creates the task', async ({ page }) => {
  test.setTimeout(3 * 60_000);
  expect(conversationId, 'draft test recorded the conversation').toBeTruthy();
  await page.goto(`/chat/${conversationId}`);
  const card = page.getByTestId('task-draft-card');
  await card.getByRole('button', { name: 'Review and create' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('Time')).toHaveValue('06:30');
  await expect(dialog.getByRole('combobox', { name: 'Days' })).toHaveText(/Weekdays/);
  await shot(page, '02-chat-draft-editor');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });
  await expect(card.getByText('Created', { exact: true })).toBeVisible();

  const rules = await briefRules(page);
  expect(rules, 'exactly one Daily brief task').toHaveLength(1);
  expect(JSON.stringify(rules[0].trigger)).toContain('30 6 * * 1-5');
  await shot(page, '03-chat-draft-created');

  await page.getByRole('link', { name: 'Open Scheduled tasks' }).click();
  // The link deep-opens the saved task's editor (`?edit=<id>`) with the drafted schedule.
  await expect(page).toHaveURL(/\/settings\/scheduled-tasks\?edit=/);
  const editor = page.getByRole('dialog');
  await expect(editor.getByLabel('Time')).toHaveValue('06:30', { timeout: 30_000 });
  await expect(editor.getByRole('combobox', { name: 'Days' })).toHaveText(/Weekdays/);
  await shot(page, '04-chat-draft-in-scheduled-tasks');
  await editor.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByText('Weekdays at', { exact: false }).first()).toBeVisible({ timeout: 30_000 });
});

// Free the one-per-workspace slot for the template-based Daily brief spec, even when a test above failed.
test.afterAll(async ({ browser }, testInfo) => {
  const { baseURL, storageState } = testInfo.project.use;
  const context = await browser.newContext({ baseURL, storageState: storageState as string | undefined });
  const page = await context.newPage();
  try {
    await page.goto('/dashboard');
    await clearBriefRules(page);
  } finally {
    await context.close();
  }
});
