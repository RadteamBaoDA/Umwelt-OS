import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { WIZARD_CASES, collectedRecordCount, connectWizardSource, enableBuiltinViaSettings, writeHeaders } from './sources-helpers';

// Opt-in end-to-end run of Scheduled tasks (Daily brief) on REAL collected data and a REAL OpenAI-compatible model.
// Configure `.env.e2e-real` (see `.env.e2e-real.example`); the file is skipped while the key, base URL or model is empty.
// SECURITY: the key goes only to the disposable stack's settings API, traces/videos are off, and screenshots are never
// taken on the Models settings page.

const ENV_FILE = path.resolve(__dirname, '../../.env.e2e-real');
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);

const KEY = process.env.E2E_REAL_API_KEY ?? '';
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? '').replace(/\/+$/, '');
const MODEL = process.env.E2E_REAL_MODEL ?? '';
const SHOTS = process.env.E2E_SHOTS_DIR ?? path.resolve(__dirname, '../../.e2e-shots/scheduled-tasks');
const REAL_TIMEOUT = 180_000;

// Real sources the brief can draw on: stories (news), markets, hazards. Weather is connected through the wizard.
const NEWS = [['bbc_world', 'BBC World News'], ['vnexpress_business', 'VnExpress Business']] as const;
const OBSERVATIONS = [['binance', 'Binance BTCUSDT price'], ['frankfurter', 'Frankfurter USD/VND'], ['usgs', 'USGS M4.5+ earthquakes (day)']] as const;

test.use({ trace: 'off', video: 'off', screenshot: 'off' });
test.describe.configure({ mode: 'serial' });
test.skip(!KEY || !BASE_URL || !MODEL, 'set E2E_REAL_BASE_URL, E2E_REAL_API_KEY and E2E_REAL_MODEL in .env.e2e-real');

/** Redacts the configured key from text that may reach an assertion message or the report. */
const mask = (text: string) => (KEY ? text.split(KEY).join('***') : text);

/** Saves a full-page screenshot for the HTML report (never on settings pages that show credentials). */
async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

/** Points the chat and brief roles at the real provider and grants remote reasoning consent; probes streaming. */
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
  const probe = await page.request.post('/api/v1/settings/models/chat/test', { headers, data: { capability: 'streaming' }, timeout: REAL_TIMEOUT });
  expect(probe.ok(), mask(`probe: ${probe.status()} ${await probe.text()}`)).toBeTruthy();
  expect((await probe.json()).result).toBe('supported');
}

type Automation = { id: string; name: string; revision: number; enabled: boolean; next_run_at?: string | null; actions: { type: string }[] };
type RunAction = { type: string; status: string; error_code: string | null; result_reference: string | null };
type Run = { id: string; status: string; actions: RunAction[] };

/** Returns the Daily brief scheduled task (a schedule rule with one generate_brief action delivering to chat). */
async function briefTask(page: Page): Promise<Automation> {
  const page1 = await (await page.request.get('/api/v1/automations')).json() as { items: Automation[] };
  const task = page1.items.find((rule) => rule.actions.length === 1 && rule.actions[0].type === 'generate_brief');
  expect(task, 'daily brief task exists').toBeTruthy();
  return task!;
}

/** Polls the task's newest run until it leaves the queued/running states; returns it. */
async function waitForRun(page: Page, automationId: string, notBefore: Set<string>): Promise<Run> {
  let latest: Run | undefined;
  await expect.poll(async () => {
    const res = await page.request.get(`/api/v1/automations/${automationId}/runs?limit=5`);
    if (!res.ok()) return 'pending';
    const items = ((await res.json()) as { items: Run[] }).items.filter((run) => !notBefore.has(run.id));
    latest = items[0];
    return latest && !['queued', 'running', 'pending'].includes(latest.status) ? latest.status : 'pending';
  }, { timeout: REAL_TIMEOUT, intervals: [2_000, 5_000], message: 'brief run finishes' }).not.toBe('pending');
  return latest!;
}

test('real sources collect data for the brief', async ({ page }) => {
  test.setTimeout(15 * 60_000);
  const outcomes: Record<string, string> = {};
  await page.goto('/settings/sources');
  const existing = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; source_id: string | null }[];
  for (const [id, label] of [...NEWS, ...OBSERVATIONS]) {
    // A built-in enabled by an earlier spec (its card is disabled) is reused as-is.
    const already = existing.find((x) => x.id === id)?.source_id ?? null;
    const result = already ? { ok: true, text: 'already enabled', sourceId: already } : await enableBuiltinViaSettings(page, id, label);
    const records = result.ok && result.sourceId ? await collectedRecordCount(page, result.sourceId, 90_000) : 0;
    outcomes[id] = result.ok ? `${records} records` : `failed: ${result.text}`;
    if (!already) await page.keyboard.press('Escape');
  }
  const weather = WIZARD_CASES.find((c) => c.id === 'open_meteo')!;
  const weatherOutcome = await connectWizardSource(page, weather, 'E2E weather Ha Noi');
  outcomes.open_meteo = weatherOutcome.ok ? 'ok' : `failed: ${weatherOutcome.text}`;
  await page.keyboard.press('Escape');
  test.info().annotations.push({ type: 'sources', description: JSON.stringify(outcomes) });
  // External providers can be down; the brief needs at least one news and one observation source with data.
  expect(NEWS.some(([id]) => /^[1-9]\d* records/.test(outcomes[id])), JSON.stringify(outcomes)).toBe(true);
  expect(OBSERVATIONS.some(([id]) => /^[1-9]\d* records/.test(outcomes[id])), JSON.stringify(outcomes)).toBe(true);
  await page.goto('/settings/sources');
  await shot(page, '01-sources');
});

test('owner sets up the Daily brief task from the template', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await configureRealModel(page);
  await page.goto('/settings/scheduled-tasks');
  await expect(page.getByRole('heading', { name: 'Scheduled tasks', exact: true })).toBeVisible();
  await shot(page, '02-scheduled-tasks-empty');
  await page.getByRole('tab', { name: 'Templates' }).click();
  await page.getByRole('button', { name: 'Set up' }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Set up daily brief')).toBeVisible();
  for (const section of ['Top stories', 'Markets', 'Weather', 'Hazards', 'Agenda', 'Source health']) {
    await expect(dialog.getByLabel(section)).toBeChecked();
  }
  await dialog.getByLabel('Extra instruction').fill('Viết bằng tiếng Việt, tối đa 150 từ.');
  await shot(page, '03-brief-editor');
  await dialog.getByRole('button', { name: 'Save' }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole('tab', { name: 'Yours' }).click();
  await expect(page.getByText('Every day at 07:00')).toBeVisible();
  const task = await briefTask(page);
  expect(task.enabled).toBe(true);
  expect(task.next_run_at, 'next run scheduled').toBeTruthy();
  await shot(page, '04-scheduled-tasks-yours');
});

test('Run now generates a brief on real data and posts it into the task thread', async ({ page }) => {
  test.setTimeout(6 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  const task = await briefTask(page);
  const before = new Set(((await (await page.request.get(`/api/v1/automations/${task.id}/runs?limit=20`)).json()) as { items: Run[] }).items.map((r) => r.id));
  await page.getByRole('button', { name: 'Run now' }).first().click();
  const run = await waitForRun(page, task.id, before);
  const action = run.actions[0];
  test.info().annotations.push({ type: 'run', description: JSON.stringify({ status: run.status, action }) });
  expect(action.status, mask(JSON.stringify(run))).toBe('succeeded');
  expect(action.result_reference ?? '').toMatch(/^brief:[0-9a-f-]{36}$/);

  const { conversation_id: conversationId } = await (await page.request.get(`/api/v1/automations/${task.id}/conversation`)).json() as { conversation_id: string | null };
  expect(conversationId, 'task thread created').toBeTruthy();
  await page.goto(`/chat/${conversationId}`);
  await expect(page.getByRole('list', { name: 'Brief sources' })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('link', { name: 'Edit task' })).toBeVisible();
  const history = page.getByRole('navigation').or(page.locator('aside')).first();
  await expect(history.getByText('Tasks')).toBeVisible();
  await shot(page, '05-task-thread-brief');

  // The posted brief carries citations and the thread is scoped to the brief's sources server-side.
  const detail = await (await page.request.get(`/api/v1/conversations/${conversationId}`)).json() as { messages: { role: string; content: string; task: { brief_citations: unknown[] } | null }[] };
  const brief = detail.messages.find((m) => m.task);
  expect(brief?.task?.brief_citations.length, 'brief cites facts').toBeGreaterThan(0);
  expect(brief?.content).toMatch(/\[\d+\]/);
});

test('follow-up question in the task thread is answered by the real model', async ({ page }) => {
  test.setTimeout(4 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  const task = await briefTask(page);
  const { conversation_id: conversationId } = await (await page.request.get(`/api/v1/automations/${task.id}/conversation`)).json() as { conversation_id: string };
  await page.goto(`/chat/${conversationId}`);
  const composer = page.getByRole('textbox').last();
  await composer.fill('Tóm tắt lại tin quan trọng nhất trong brief hôm nay trong 2 câu.');
  await composer.press('Enter');
  await expect.poll(async () => {
    const detail = await (await page.request.get(`/api/v1/conversations/${conversationId}`)).json() as { messages: { role: string; content: string; task: unknown }[] };
    const answers = detail.messages.filter((m) => m.role === 'assistant' && !m.task && m.content.trim().length > 20);
    return answers.length;
  }, { timeout: REAL_TIMEOUT, intervals: [3_000], message: 'real model answers in thread' }).toBeGreaterThan(0);
  await shot(page, '06-task-thread-followup');
  expect((await page.content()).includes(KEY), 'API key never rendered').toBe(false);
});

test('dashboard daily brief gadget shows the brief and links to the thread', async ({ page }) => {
  test.setTimeout(2 * 60_000);
  await page.goto('/dashboard');
  const link = page.getByRole('link', { name: /Open full brief/i });
  await expect(link).toBeVisible({ timeout: 30_000 });
  await shot(page, '07-dashboard-brief');
  await link.click();
  await expect(page).toHaveURL(/\/chat\/|\/settings\/scheduled-tasks/);
});

test('pausing the task disables the schedule; cleanup removes the provider', async ({ page }) => {
  test.setTimeout(2 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  await page.getByRole('button', { name: 'Pause' }).first().click();
  await expect(page.getByText('Paused').first()).toBeVisible();
  const task = await briefTask(page);
  expect(task.enabled).toBe(false);
  await shot(page, '08-task-paused');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const res = await page.request.put('/api/v1/settings/ai', { headers, data: { providers: [], defaults: {}, privacy: { allow_remote_reasoning: false, reasoning_destinations: [], allow_remote_embeddings: false, embedding_destinations: [] }, request_timeout_seconds: 30, expected_revision: current.configuration_revision } });
  expect(res.ok(), mask(`remove provider: ${res.status()}`)).toBeTruthy();
});
