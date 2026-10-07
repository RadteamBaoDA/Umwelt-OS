import { test, expect } from './fixtures';

test('owner sees truthful integration status', async ({ page }) => {
  await page.goto('/settings/system');
  await expect(page.getByRole('heading', { name: 'System status' })).toBeVisible();
  await expect(page.locator('.status-item').filter({ hasText: 'Database' })).toContainText('healthy');
  await expect(page.locator('.status-item').filter({ hasText: 'Worker' })).toContainText('healthy');
  // The test stack wires OmniRoute to the fake model service, so it is configured but never probed.
  await expect(page.getByText('OmniRoute configured · connectivity not tested')).toBeVisible();
  await page.goto('/settings/sources');
  await expect(page.getByText('No sources yet.')).toBeVisible();
});
