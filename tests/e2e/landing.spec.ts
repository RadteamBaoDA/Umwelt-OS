import { test, expect } from './fixtures';

test('public preview follows selected sources and questions without calling live AI', async ({ page }) => {
  await page.goto('/?lang=en-us');
  await expect(page.getByRole('heading', { name: /Connect your data/ })).toBeVisible();

  const website = page.getByRole('button', { name: 'Website', exact: true });
  const feed = page.getByRole('button', { name: 'RSS / news', exact: true });
  const document = page.getByRole('button', { name: 'Document', exact: true });
  await expect(website).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Open Research document reference' }).click();
  await expect(page.getByText('This document describes review steps for organizing collected information.')).toBeVisible();
  await page.getByRole('button', { name: 'Close' }).click();

  await website.click();
  await expect(website).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByText('The website guide explains how a public page can be collected with its original link.')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Open Website guide reference' })).toHaveCount(0);

  await feed.click();
  await page.getByRole('button', { name: 'Compare sources' }).click();
  await expect(page.getByText('Choose at least two sources to compare them.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Open .* reference/ })).toHaveCount(0);

  await document.click();
  await expect(page.getByText('Choose at least one source to explore its answer.')).toBeVisible();
});
