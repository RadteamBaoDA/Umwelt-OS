import { test, expect } from './fixtures';

test('onboarding topic chips toggle idempotently', async ({ page }) => {
  await page.goto('/onboarding');
  // Move to the Data sources step if the saved step is earlier; chips live there.
  const chip = page.getByRole('button', { name: 'Technology', exact: true });
  if (!(await chip.isVisible())) {
    await page.getByRole('button', { name: /Data sources/ }).click();
  }
  await expect(chip).toBeVisible();
  const before = await chip.getAttribute('aria-pressed');
  await chip.click();
  await expect(chip).not.toHaveAttribute('aria-pressed', before ?? 'false');
  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', before ?? 'false');
  const topics = await (await page.request.get('/api/v1/topics?limit=100')).json();
  expect(topics.items.filter((t: { name: string }) => t.name === 'Technology').length).toBeLessThanOrEqual(1);
});
