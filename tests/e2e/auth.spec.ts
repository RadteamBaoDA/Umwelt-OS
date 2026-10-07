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
