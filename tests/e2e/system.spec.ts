import { test, expect } from './fixtures';

test('owner sees truthful integration status', async ({ page }) => {
  await page.goto('/settings/system');
  await expect(page.getByRole('heading', { name: 'System status' })).toBeVisible();
  await expect(page.locator('.status-item').filter({ hasText: 'Database' })).toContainText('healthy');
  await expect(page.locator('.status-item').filter({ has: page.getByText('Worker', { exact: true }) })).toContainText('healthy');
  await expect(page.locator('.status-item').filter({ has: page.getByText('Chat worker', { exact: true }) })).toContainText('healthy');
  // The test stack wires the model gateway to the fake model service, so it is configured but never probed.
  await expect(page.locator('.status-item').filter({ hasText: 'Model gateway' })).toContainText(
    'configured · connectivity not tested',
  );
  await page.goto('/settings/sources');
  await expect(page.getByText('No sources yet.')).toBeVisible();
});
