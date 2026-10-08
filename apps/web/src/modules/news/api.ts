import { apiRequest, csrfHeaders } from '@/core/api';
import type { Topic, TopicCreate, TopicEntityOption, TopicPage, TopicUpdate } from './types';

/** Fetches one bounded owner page, preserving the active filter in cursor scope. */
export function fetchTopics(options: { isActive?: boolean; limit?: number; cursor?: string; signal?: AbortSignal } = {}): Promise<TopicPage> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.isActive !== undefined) params.set('is_active', String(options.isActive));
  if (options.cursor) params.set('cursor', options.cursor);
  return apiRequest<TopicPage>(`/api/v1/topics?${params}`, { signal: options.signal });
}

/** Reads every owner topic (bounded to 10 pages) so state never depends on one page. */
export async function fetchAllTopics(): Promise<Topic[]> {
  const items: Topic[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < 10; page += 1) {
    const result = await fetchTopics({ limit: 100, cursor });
    items.push(...result.items);
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
  }
  return items;
}

/** Fetches a single current owner topic for conflict review. */
export function getTopic(id: string): Promise<Topic> {
  return apiRequest<Topic>(`/api/v1/topics/${id}`);
}

/** Creates a topic with mandatory trusted CSRF credentials on this write. */
export function createTopic(payload: TopicCreate, csrfToken: string): Promise<Topic> {
  return apiRequest<Topic>('/api/v1/topics', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}

/** Applies a revision-fenced topic update with mandatory trusted CSRF credentials. */
export function updateTopic(id: string, payload: TopicUpdate, csrfToken: string): Promise<Topic> {
  return apiRequest<Topic>(`/api/v1/topics/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}

/** Tombstones the profile only at its last observed revision. */
export function deleteTopic(id: string, expectedRevision: number, csrfToken: string): Promise<void> {
  return apiRequest<void>(`/api/v1/topics/${id}?expected_revision=${expectedRevision}`, {
    method: 'DELETE', headers: csrfHeaders(csrfToken),
  });
}

/** Searches canonical entity records using the knowledge module's public API. */
export function searchTopicEntities(query: string): Promise<{ items: TopicEntityOption[]; next_cursor: string | null }> {
  const params = new URLSearchParams({ limit: '50' });
  if (query.trim()) params.set('q', query.trim());
  return apiRequest<{ items: TopicEntityOption[]; next_cursor: string | null }>(`/api/v1/entities?${params}`);
}
