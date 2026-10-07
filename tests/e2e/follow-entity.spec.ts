import { test, expect } from './fixtures';

type TopicItem = { id: string; entity_ids: string[]; is_active: boolean; revision: number };

test('owner follows an entity and sees the topic', async ({ page }) => {
  await page.goto('/');
  const session = await (await page.request.get('/api/v1/auth/session')).json();
  const headers = { 'X-CSRF-Token': session.csrfToken };
  const created = await page.request.post('/api/v1/entities', {
    headers,
    data: { name: `E2E Follow Co ${Date.now()}`, type: 'organization' },
  });
  expect(created.ok()).toBeTruthy();
  const entity = await created.json();
  const linkedTopics = async (): Promise<TopicItem[]> => {
    const topics = await (await page.request.get('/api/v1/topics?limit=100')).json();
    return topics.items.filter((t: TopicItem) => t.entity_ids.includes(entity.id));
  };

  await page.goto(`/knowledge/entities/${entity.id}`);
  await page.getByRole('button', { name: 'Follow', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Following' })).toBeVisible();

  // State comes from the server, not local state.
  await page.reload();
  await expect(page.getByRole('button', { name: 'Following' })).toBeVisible();
  expect(await linkedTopics()).toHaveLength(1);

  // Deactivated topic is reactivated, not duplicated.
  const [topic] = await linkedTopics();
  const patched = await page.request.patch(`/api/v1/topics/${topic.id}`, {
    headers,
    data: { expected_revision: topic.revision, is_active: false },
  });
  expect(patched.ok()).toBeTruthy();
  await page.reload();
  await page.getByRole('button', { name: 'Follow', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Following' })).toBeVisible();
  const after = await linkedTopics();
  expect(after).toHaveLength(1);
  expect(after[0].is_active).toBe(true);
});
