import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';

// Web search UI (P15 W4). All network is mocked via page.route; nothing reaches a real backend or provider.
// Mirrors WEB_SEARCH_SEND_ENABLED (modules/chat/api.ts), enabled with W2; the toggle-off case is skipped while it is true.
const SEND_ENABLED = true;

type Cit = Record<string, unknown>;
const web = (url: string, title: string, quote = 'snippet'): Cit => ({ sourceType: 'web', url, title, quote, provider: 'tavily', retrievedAt: '2026-10-07T00:00:00Z' });

async function mockChat(page: Page, opts: { configured?: boolean; citations?: Cit[]; webSearch?: unknown } = {}) {
  const now = '2026-10-07T00:00:00Z';
  await page.route('**/api/v1/settings/ai', (route) => route.fulfill({ json: {
    web_search_provider: opts.configured ? 'tavily' : 'none',
    web_search_credential_configured: Boolean(opts.configured),
    privacy: { allow_remote_web_search: Boolean(opts.configured) },
  } }));
  const conversation = { id: 'c1', title: 'Web chat', context_kind: null, context_resource_id: null, pinned: false, archived: false, created_at: now, updated_at: now, metadata: {} };
  const message = (id: string, role: string, content: string, extra: object = {}) => ({ id, conversation_id: 'c1', role, content, client_request_id: null, model_identity: null, citations: [], response_id: null, revision_of_message_id: null, created_at: now, ...extra });
  await page.route('**/api/v1/conversations/c1', (route) => route.fulfill({ json: {
    ...conversation, active_response_id: null,
    messages: [message('m1', 'user', 'hello'), message('m2', 'assistant', 'answer', { citations: opts.citations ?? [], web_search: opts.webSearch ?? null })],
  } }));
  await page.route(/\/api\/v1\/conversations(\?.*)?$/, (route) => route.fulfill({ json: [conversation] }));
}

async function openChat(page: Page) {
  await page.goto('/chat');
  await page.getByText('Web chat').first().click();
}

test.describe('web search toggle', () => {
  test('is absent in the quick-chat drawer', async ({ page }) => {
    await mockChat(page, { configured: true });
    await page.goto('/');
    await page.getByRole('button', { name: /chat/i }).first().click();
    await expect(page.locator('#chat-web-search')).toHaveCount(0);
  });

  test('is disabled with an explanation when unconfigured', async ({ page }) => {
    test.skip(!SEND_ENABLED, 'toggle hidden until W3 enables WEB_SEARCH_SEND_ENABLED');
    await mockChat(page, { configured: false });
    await page.goto('/chat');
    await expect(page.locator('#chat-web-search')).toBeDisabled();
    await expect(page.locator('#chat-web-search-help')).toContainText('unavailable');
  });

  test('is hidden while the send flag is off', async ({ page }) => {
    test.skip(SEND_ENABLED, 'only meaningful while WEB_SEARCH_SEND_ENABLED is false');
    await mockChat(page, { configured: true });
    await page.goto('/chat');
    await expect(page.locator('#chat-web-search')).toHaveCount(0);
  });

  test('sends web_search on the ticked message', async ({ page }) => {
    test.skip(!SEND_ENABLED, 'toggle hidden until W3 enables WEB_SEARCH_SEND_ENABLED');
    await mockChat(page, { configured: true });
    const bodies: Array<Record<string, unknown>> = [];
    await page.route('**/api/v1/conversations/c1/messages', async (route) => {
      bodies.push(route.request().postDataJSON());
      await route.fulfill({ status: 500, json: { detail: 'stop' } });
    });
    await openChat(page);
    await page.locator('#chat-web-search').check();
    await page.getByRole('textbox', { name: /Ask a question about your knowledge/ }).fill('q1');
    await page.keyboard.press('Enter');
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].web_search).toBe(true);
  });
});

test.describe('web citations (EN)', () => {
  test('renders hostile title and snippet as plain text', async ({ page }) => {
    await mockChat(page, { citations: [web('https://example.com/a', '<img src=x onerror=alert(1)>', '[x](javascript:alert(1))')] });
    await openChat(page);
    await expect(page.getByText('<img src=x onerror=alert(1)>').first()).toBeVisible();
    await expect(page.getByText('[x](javascript:alert(1))').first()).toBeVisible();
    await expect(page.locator('main img[src="x"]')).toHaveCount(0);
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  });

  test('renders no anchor for javascript: or credentialed URLs', async ({ page }) => {
    await mockChat(page, { citations: [web('javascript:alert(1)', 'js'), web('https://user:pass@evil.example/', 'cred')] });
    await openChat(page);
    await expect(page.getByText('js').first()).toBeVisible();
    await expect(page.locator('a[href^="javascript:"], a[href*="user:pass"], a[href*="evil.example"]')).toHaveCount(0);
  });

  // Same-URL dedupe happens server-side before numbering (test_web_citations_with_same_url_are_deduped); the client renders stored citations 1:1 so [n] markers stay aligned.

  const reasons: Array<[string, string, string]> = [
    ['not_configured', 'skipped', 'not configured'],
    ['query_too_long', 'skipped', 'too long'],
    ['local_only_context', 'skipped', 'local-only'],
    ['daily_limit', 'skipped', 'daily web search limit'],
    ['run_inactive', 'skipped', 'stopped before the search ran'],
    ['timeout', 'unavailable', 'timed out'],
    ['provider_error', 'unavailable', 'returned an error'],
    ['network_denied', 'unavailable', 'blocked'],
  ];
  for (const [reason, status, text] of reasons) {
    test(`notice for ${reason}`, async ({ page }) => {
      await mockChat(page, { webSearch: { status, reason, result_count: 0 } });
      await openChat(page);
      await expect(page.getByTestId('web-search-notice')).toContainText(text);
    });
  }
});

// Assumes the owner preference is not persisted so the browser locale selects Vietnamese.
test.describe('web search notices (VI)', () => {
  test.use({ locale: 'vi-VN' });
  const reasons: Array<[string, string, string]> = [
    ['not_configured', 'skipped', 'Chưa được cấu hình'],
    ['query_too_long', 'skipped', 'quá dài'],
    ['local_only_context', 'skipped', 'cục bộ'],
    ['daily_limit', 'skipped', 'trong ngày'],
    ['run_inactive', 'skipped', 'dừng trước khi tìm kiếm'],
    ['timeout', 'unavailable', 'hết thời gian chờ'],
    ['provider_error', 'unavailable', 'trả về lỗi'],
    ['network_denied', 'unavailable', 'bị chặn'],
  ];
  for (const [reason, status, text] of reasons) {
    test(`thông báo ${reason}`, async ({ page }) => {
      await mockChat(page, { webSearch: { status, reason, result_count: 0 } });
      await openChat(page);
      await expect(page.getByTestId('web-search-notice')).toContainText(text);
    });
  }
});
