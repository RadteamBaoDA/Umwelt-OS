import { test, expect } from './fixtures';

test('owner sees truthful integration status', async ({ page }) => {
  await page.goto('/settings/system');
  await expect(page.getByRole('heading', { name: 'System status' })).toBeVisible();
  await expect(page.locator('.status-item').filter({ hasText: 'Database' })).toContainText('healthy');
  await expect(page.locator('.status-item').filter({ has: page.getByText('Worker', { exact: true }) })).toContainText('healthy');
  await expect(page.locator('.status-item').filter({ has: page.getByText('Chat worker', { exact: true }) })).toContainText('healthy');
  // The gateway counts as configured only once an owner saves a provider (no env bootstrap); a fresh stack has none.
  await expect(page.locator('.status-item').filter({ hasText: 'Model gateway' })).toContainText('Model gateway is not configured');
});
