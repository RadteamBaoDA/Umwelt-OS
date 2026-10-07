import { test, expect } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

test('owner can set up, log in, and log out', async ({ page }) => {
  const setupToken = process.env.E2E_SETUP_TOKEN;
  if (!setupToken) throw new Error('E2E_SETUP_TOKEN must be set for the disposable test app');

  await page.goto('/');
  await page.getByLabel('Setup token').fill(setupToken);
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByLabel('Confirm password').fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Create owner' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'User menu' }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await mkdir('playwright/.auth', { recursive: true });
  await page.context().storageState({ path: 'playwright/.auth/owner.json' });
});

test('sign out stays a sign out when a request 401s before the logout reply', async ({ page }) => {
  // Logout revokes the session server-side; any protected request answered after that (realtime probe,
  // refetch) 401s and can reach the shell before the logout reply does. That must not read as "expired".
  await page.goto('/login');
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  let releaseLogout = () => {};
  const held = new Promise<void>((resolve) => { releaseLogout = resolve; });
  await page.route('**/api/v1/auth/logout', async (route) => {
    const response = await route.fetch();
    await held;
    await route.fulfill({ response });
  });
  const logoutSent = page.waitForRequest('**/api/v1/auth/logout');
  await page.getByRole('button', { name: 'User menu' }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await logoutSent;
  await page.evaluate(() => window.dispatchEvent(new Event('bbd:unauthorized')));
  releaseLogout();
  await expect(page).toHaveURL(/\/login$/);
});
