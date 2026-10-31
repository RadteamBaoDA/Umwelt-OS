import { test, expect } from './fixtures';

test('owner sees truthful integration status', async ({ page }) => {
  await page.goto('/settings/system');
  await expect(page.getByRole('heading', { name: 'System status' })).toBeVisible();
  await expect(page.locator('.status-item').filter({ hasText: 'Database' })).toContainText('Healthy');
  // Workers report through heartbeats; on a freshly started stack the first beat can land after the page loads.
  await expect(async () => {
    await page.reload();
    await expect(page.locator('.status-item').filter({ has: page.getByText('Worker', { exact: true }) })).toContainText('Healthy', { timeout: 3_000 });
    await expect(page.locator('.status-item').filter({ has: page.getByText('Chat worker', { exact: true }) })).toContainText('Healthy', { timeout: 3_000 });
  }).toPass({ timeout: 90_000 });
  // The gateway counts as configured only once an owner saves a provider (no env bootstrap); a fresh stack has none.
  await expect(page.locator('.status-item').filter({ hasText: 'Model gateway' })).toContainText('Not configured');
});
