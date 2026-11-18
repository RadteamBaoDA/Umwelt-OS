import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { writeHeaders } from './sources-helpers';

// Opt-in end-to-end run of the Topic watch scheduled task on REAL web search (the `real-search` compose profile's
// SearXNG at http://searxng:8080) and a REAL OpenAI-compatible model. Needs `.env.e2e-real` (see
// `.env.e2e-real.example`) and E2E_REAL_SEARCH=1 (which defaults E2E_WEB_SEARCH_ALLOWED_CIDRS to the Docker
// network range) or an explicit E2E_WEB_SEARCH_ALLOWED_CIDRS; the file is skipped otherwise.
// SECURITY: the key goes only to the disposable stack's settings API, traces/videos are off, and screenshots are never
// taken on the Models settings page.

const ENV_FILE = path.resolve(__dirname, '../../.env.e2e-real');
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);

const KEY = process.env.E2E_REAL_API_KEY ?? '';
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? '').replace(/\/+$/, '');
const MODEL = process.env.E2E_REAL_MODEL ?? '';
// The stack only admits the SearXNG container when its CIDR is allowed (WEB_SEARCH_ALLOWED_CIDRS, see
// docker-compose.test.yml); E2E_REAL_SEARCH=1 supplies the default Docker range.
if (process.env.E2E_REAL_SEARCH === '1') process.env.E2E_WEB_SEARCH_ALLOWED_CIDRS ??= '["172.16.0.0/12"]';
const CIDRS = process.env.E2E_WEB_SEARCH_ALLOWED_CIDRS ?? '';
const SEARXNG = 'http://searxng:8080';
const SHOTS = process.env.E2E_SHOTS_DIR ?? path.resolve(__dirname, '../../test-results/topic-shots');
const REAL_TIMEOUT = 360_000;
const TOPIC = 'Thị trường xe điện Việt Nam';

test.use({ trace: 'off', video: 'off', screenshot: 'off' });
test.describe.configure({ mode: 'serial' });
test.skip(!KEY || !BASE_URL || !MODEL || !CIDRS, 'set E2E_REAL_* in .env.e2e-real and E2E_REAL_SEARCH=1 or E2E_WEB_SEARCH_ALLOWED_CIDRS (real-search profile)');

/** Redacts the configured key from text that may reach an assertion message or the report. */
const mask = (text: string) => (KEY ? text.split(KEY).join('***') : text);

/** Saves a full-page screenshot for the HTML report (never on settings pages that show credentials). */
async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true, animations: 'disabled' });
}

/**
 * Points the chat, brief and background roles at the real provider, configures SearXNG web search and grants the
 * remote reasoning and remote web search consents (each tied to its saved destination id), then probes streaming.
 */
async function configureRealModelAndSearch(page: Page) {
  await page.goto('/dashboard');
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const role = { model: MODEL, destination: 'remote', provider_id: 'real' };
  const defaults = { chat: role, brief: role, background: role };
  const search = { web_search_provider: 'searxng', web_search_endpoint: SEARXNG };
  const created = await page.request.put('/api/v1/settings/ai', { headers, data: {
    defaults, request_timeout_seconds: 120, ...search,
    providers: [{ id: 'real', kind: 'custom', label: 'real', base_url: BASE_URL, credential_action: 'replaced', api_key: KEY, models: [{ id: MODEL, type: 'llm' }] }],
    expected_revision: current.configuration_revision,
  } });
  expect(created.ok(), mask(`create provider + search: ${created.status()} ${await created.text()}`)).toBeTruthy();
  const body = await created.json();
  const destination = body.providers.find((p: { id: string }) => p.id === 'real').destination_id;
  expect(body.web_search_destination_id, 'searxng destination registered').toBeTruthy();
  const saved = await page.request.put('/api/v1/settings/ai', { headers, data: {
    defaults, request_timeout_seconds: 120, ...search,
    privacy: {
      allow_remote_reasoning: true, reasoning_destinations: [destination], allow_remote_embeddings: false, embedding_destinations: [],
      allow_remote_web_search: true, web_search_destinations: [body.web_search_destination_id],
    },
    expected_revision: body.configuration_revision,
  } });
  expect(saved.ok(), mask(`grant consent: ${saved.status()} ${await saved.text()}`)).toBeTruthy();
  const probe = await page.request.post('/api/v1/settings/models/chat/test', { headers, data: { capability: 'streaming' }, timeout: REAL_TIMEOUT });
  expect(probe.ok(), mask(`probe: ${probe.status()} ${await probe.text()}`)).toBeTruthy();
  expect((await probe.json()).result).toBe('supported');
}

type Automation = { id: string; name: string; revision: number; actions: { type: string; source_id?: string | null }[] };
type RunAction = { type: string; status: string; error_code: string | null; result_reference: string | null };
type Run = { id: string; status: string; actions: RunAction[] };
type Message = {
  role: string;
  content: string;
  citations?: unknown[];
  task: { task_kind: string; items?: { document_id: string; title: string }[] } | null;
};

/** Title of the first collected item, set by the Run now test and looked for in the dashboard Feed gadget. */
let firstItemTitle = '';

/** The Topic watch row of the Yours table: the only row that shows the topic text (a paused Daily brief may exist). */
const topicRow = (page: Page) => page.getByRole('row').filter({ hasText: TOPIC });

/** Returns the Topic watch task (a schedule rule with one collect_source action). */
async function topicTask(page: Page): Promise<Automation> {
  const list = await (await page.request.get('/api/v1/automations')).json() as { items: Automation[] };
  const task = list.items.find((rule) => rule.actions.length === 1 && rule.actions[0].type === 'collect_source');
  expect(task, 'topic watch task exists').toBeTruthy();
  return task!;
}

/** Reads the task thread's messages, or null before the first post. */
async function threadMessages(page: Page, automationId: string): Promise<{ id: string; messages: Message[] } | null> {
  const { conversation_id: id } = await (await page.request.get(`/api/v1/automations/${automationId}/conversation`)).json() as { conversation_id: string | null };
  if (!id) return null;
  const detail = await (await page.request.get(`/api/v1/conversations/${id}`)).json() as { messages: Message[] };
  return { id, messages: detail.messages };
}

/** Polls until the newest run's action is `settled:` (or a topic-watch post exists); returns the run's action. */
async function waitForSettled(page: Page, automationId: string, notBefore: Set<string>): Promise<RunAction | undefined> {
  let action: RunAction | undefined;
  await expect.poll(async () => {
    const res = await page.request.get(`/api/v1/automations/${automationId}/runs?limit=5`);
    if (res.ok()) {
      const runs = ((await res.json()) as { items: Run[] }).items.filter((run) => !notBefore.has(run.id));
      action = runs[0]?.actions[0];
      if (action?.result_reference?.startsWith('settled:') || action?.status === 'failed') return 'done';
    }
    const thread = await threadMessages(page, automationId);
    return thread?.messages.some((m) => m.task?.task_kind === 'topic_watch') ? 'done' : 'pending';
  }, { timeout: REAL_TIMEOUT, intervals: [3_000, 5_000], message: 'topic watch run settles' }).toBe('done');
  return action;
}

test('owner creates a Topic watch from the template with consent', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await configureRealModelAndSearch(page);
  await page.goto('/settings/scheduled-tasks');
  await page.getByRole('tab', { name: 'Templates' }).click();
  await shot(page, '01-templates');
  // Scope to the Topic watch template card: the Daily brief card has its own "Set up" button.
  await page.getByRole('listitem').filter({ hasText: 'Topic watch' }).getByRole('button', { name: 'Set up' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Set up topic watch')).toBeVisible();
  await dialog.getByLabel('Topic', { exact: true }).fill(TOPIC);
  await dialog.getByLabel('Max items per run').fill('3');
  const save = dialog.getByRole('button', { name: 'Save' });
  await expect(save).toBeDisabled();
  await expect(dialog.getByText(/Estimated up to .* tokens per run, capped at/)).toBeVisible();
  await dialog.getByRole('checkbox', { name: 'I allow this task to run unattended' }).click();
  await expect(save).toBeEnabled();
  await shot(page, '02-topic-editor');
  await save.click();
  await expect(dialog).toBeHidden();
  await page.getByRole('tab', { name: 'Yours' }).click();
  await expect(page.getByText(TOPIC).first()).toBeVisible();
  const task = await topicTask(page);
  expect(task.actions[0].source_id, 'task-owned source assigned').toBeTruthy();
  await shot(page, '03-yours');
});

test('Run now collects real pages and posts them into the task thread', async ({ page }) => {
  test.setTimeout(8 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  const task = await topicTask(page);
  const before = new Set(((await (await page.request.get(`/api/v1/automations/${task.id}/runs?limit=20`)).json()) as { items: Run[] }).items.map((r) => r.id));
  await topicRow(page).getByRole('button', { name: 'Run now' }).click();
  const action = await waitForSettled(page, task.id, before);
  test.info().annotations.push({ type: 'run', description: JSON.stringify(action) });
  expect(action?.status, mask(JSON.stringify(action))).not.toBe('failed');

  const thread = await threadMessages(page, task.id);
  expect(thread, 'task thread created').toBeTruthy();
  const post = thread!.messages.find((m) => m.task?.task_kind === 'topic_watch');
  expect(post?.task?.items?.length ?? 0, 'at least one collected item').toBeGreaterThanOrEqual(1);
  firstItemTitle = post!.task!.items![0].title;
  await page.goto(`/chat/${thread!.id}`);
  await expect(page.getByTestId('task-topic-message').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-testid="task-topic-message"] a[href^="/knowledge/documents/"]').first()).toBeVisible();
  await shot(page, '04-topic-thread');
});

test('follow-up question in the topic thread is answered', async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  const task = await topicTask(page);
  const thread = await threadMessages(page, task.id);
  await page.goto(`/chat/${thread!.id}`);
  const composer = page.getByRole('textbox').last();
  await composer.fill('Tóm tắt các mục đã thu thập trong 2 câu.');
  await composer.press('Enter');
  const answers = async () => (await threadMessages(page, task.id))?.messages.filter((m) => m.role === 'assistant' && !m.task && m.content.trim().length > 20) ?? [];
  await expect.poll(async () => (await answers()).length, { timeout: REAL_TIMEOUT, intervals: [3_000], message: 'real model answers in thread' }).toBeGreaterThan(0);
  // Grounded in the pinned topic source: the answer carries citations to the collected documents.
  expect((await answers()).at(-1)?.citations?.length ?? 0, 'answer cites the collected documents').toBeGreaterThan(0);
  await shot(page, '05-topic-followup');
  expect((await page.content()).includes(KEY), 'API key never rendered').toBe(false);
});

test('Show on dashboard adds a Feed gadget that lists an item', async ({ page }) => {
  test.setTimeout(3 * 60_000);
  // The deep link opens Add gadget only on an existing dashboard; a fresh install has none yet.
  await page.goto('/dashboard');
  const dashboards = await (await page.request.get('/api/v1/dashboards')).json() as { items?: unknown[] } | unknown[];
  if ((Array.isArray(dashboards) ? dashboards : dashboards.items ?? []).length === 0) {
    await page.getByRole('button', { name: 'Create a dashboard' }).click();
    await page.getByLabel('Dashboard name').fill('E2E home');
    await page.getByRole('dialog').getByRole('button', { name: 'Create', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Edit layout' })).toBeEnabled({ timeout: 30_000 });
  }
  await page.goto('/settings/scheduled-tasks');
  await topicRow(page).getByRole('link', { name: 'Show on dashboard' }).click();
  await expect(page).toHaveURL(/\/dashboard/);
  const dialog = page.getByRole('dialog', { name: 'Add gadget to dashboard' });
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  const add = dialog.getByRole('button', { name: 'Add gadget', exact: true });
  await expect(add).toBeEnabled({ timeout: 60_000 });
  await add.click();
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  const save = page.getByRole('button', { name: 'Save layout' });
  if (await save.isEnabled().catch(() => false)) await save.click();
  const done = page.getByRole('button', { name: 'Done editing' });
  if (await done.isVisible().catch(() => false)) await done.click();
  // Feed rows: a lead article plus list items (same selector as dashboard-datasources.spec.ts).
  expect(firstItemTitle, 'Run now test recorded an item title').toBeTruthy();
  await expect(page.getByText(firstItemTitle, { exact: false }).first()).toBeVisible({ timeout: 60_000 });
  await shot(page, '06-dashboard-feed');
});

test('deleting the task keeps the items by default and archives its source', async ({ page }) => {
  test.setTimeout(2 * 60_000);
  await page.goto('/settings/scheduled-tasks');
  const task = await topicTask(page);
  const sourceId = task.actions[0].source_id!;
  await topicRow(page).getByRole('button', { name: 'Delete', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const purge = dialog.getByRole('checkbox', { name: /Also delete the .* collected items/ });
  await expect(purge).not.toBeChecked();
  await shot(page, '07-delete-dialog');
  await dialog.getByRole('button', { name: 'Delete task' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(TOPIC)).toHaveCount(0);
  const source = await page.request.get(`/api/v1/sources/${sourceId}`);
  expect(source.ok(), 'source still readable (archived, not purged)').toBeTruthy();
  expect((await source.json()).status, 'source archived').toBe('archived');
  // Keep: the archived source's collected items stay stored and searchable.
  const docs = await page.request.get(`/api/v1/documents?source_id=${sourceId}&limit=100`);
  expect(docs.ok(), 'documents of the archived source readable').toBeTruthy();
  expect(((await docs.json()) as { items: unknown[] }).items.length, 'items kept after delete').toBeGreaterThan(0);
  const headers = await writeHeaders(page);
  const current = await (await page.request.get('/api/v1/settings/ai')).json();
  const res = await page.request.put('/api/v1/settings/ai', { headers, data: { providers: [], defaults: {}, privacy: { allow_remote_reasoning: false, reasoning_destinations: [], allow_remote_embeddings: false, embedding_destinations: [], allow_remote_web_search: false, web_search_destinations: [] }, web_search_provider: 'none', request_timeout_seconds: 30, expected_revision: current.configuration_revision } });
  expect(res.ok(), mask(`cleanup settings: ${res.status()}`)).toBeTruthy();
});
