import { test, expect } from './fixtures';

/** The shell must report realtime "Connected" (sr-only status; the visible indicator shows only when degraded) once the event stream opens through the web origin. */
test('realtime indicator reaches Connected after sign-in', async ({ page }) => {
  const streams: string[] = [];
  // EventSource requests that never receive headers show up only as 'request', not 'response'.
  page.on('request', (r) => {
    if (r.url().includes('/api/v1/realtime/')) streams.push(`REQ ${new URL(r.url()).pathname}`);
  });
  page.on('response', (r) => {
    if (r.url().includes('/api/v1/realtime/')) streams.push(`${r.status()} ${new URL(r.url()).pathname} ${r.headers()['content-type'] ?? ''}`);
  });
  page.on('requestfailed', (r) => {
    if (r.url().includes('/api/v1/realtime/')) streams.push(`FAILED ${new URL(r.url()).pathname} ${r.failure()?.errorText ?? ''}`);
  });
  await page.goto('/dashboard');
  try {
    // The visible indicator only renders after a 5 s degraded state; healthy state is the sr-only status text.
    await expect(page.getByRole('status').filter({ hasText: /^Realtime: Connected$/ })).toHaveCount(1, { timeout: 10_000 });
    await expect(page.locator('.connection-indicator')).toHaveCount(0);
  } finally {
    // Diagnostic for the "stuck at Connecting" report: which realtime calls answered, and how.
    console.log(`REALTIME :: ${await page.getByRole('status').filter({ hasText: /^Realtime:/ }).first().textContent().catch(() => 'no status')} :: ${streams.join(' | ') || 'no realtime responses'}`);
  }
});
