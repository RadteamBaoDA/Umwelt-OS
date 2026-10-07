import { test, expect } from './fixtures';

type Onboarding = { configuration_revision: number; current_step: string; completed_at: string | null };
type TopicItem = { name: string; is_active: boolean };

test('onboarding topic chips toggle idempotently', async ({ page }) => {
  await page.goto('/');
  const session = await (await page.request.get('/api/v1/auth/session')).json();
  const headers = { 'X-CSRF-Token': session.csrfToken };
  const read = async (): Promise<Onboarding> => (await page.request.get('/api/v1/settings/onboarding')).json();
  let state = await read();
  test.skip(state.completed_at !== null, 'onboarding completed; chips unreachable');
  // Chips live on `sources`; the API allows moving back freely but only one step forward.
  const put = async (step: string) => {
    const res = await page.request.put('/api/v1/settings/onboarding', { headers, data: { expected_revision: state.configuration_revision, current_step: step } });
    expect(res.ok()).toBeTruthy();
    state = await read();
  };
  if (state.current_step === 'ai_privacy') await put('capability');
  if (state.current_step !== 'sources') await put('sources');
  await page.goto('/onboarding');
  const chip = page.getByRole('button', { name: 'Technology', exact: true });
  await expect(chip).toBeVisible();
  const before = (await chip.getAttribute('aria-pressed')) ?? 'false';
  const flipped = before === 'true' ? 'false' : 'true';
  const matches = async (): Promise<TopicItem[]> => {
    const topics = await (await page.request.get('/api/v1/topics?limit=100')).json();
    return topics.items.filter((t: TopicItem) => t.name.toLowerCase() === 'technology');
  };
  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', flipped);
  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', before);
  // Select again so exactly one active match must exist, then restore.
  if (before === 'false') await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', 'true');
  expect((await matches()).filter((t) => t.is_active)).toHaveLength(1);
  if (before === 'false') {
    await chip.click();
    await expect(chip).toHaveAttribute('aria-pressed', 'false');
  }
});
