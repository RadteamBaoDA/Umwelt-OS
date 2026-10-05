import { apiRequest } from '@/core/api';
import type { StoryDetail, StoryPage, TrendPage } from './story-types';

/** Fetch a bounded current story page for selected owner sources. */
export function fetchStories(
  sourceIds: string[], options: { limit?: number; cursor?: string; topicId?: string; entityId?: string; q?: string } = {},
): Promise<StoryPage> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
  sourceIds.forEach((id) => params.append('source_ids', id));
  if (options.cursor) params.set('cursor', options.cursor);
  if (options.topicId) params.set('topic_id', options.topicId);
  if (options.entityId) params.set('entity_id', options.entityId);
  if (options.q) params.set('q', options.q);
  return apiRequest<StoryPage>(`/api/v1/stories?${params.toString()}`);
}

/** Fetch one story only while current authorized evidence remains available. */
export function fetchStory(storyId: string, sourceIds: string[], evidenceCursor?: string): Promise<StoryDetail> {
  const params = new URLSearchParams();
  sourceIds.forEach((id) => params.append('source_ids', id));
  if (evidenceCursor) params.set('evidence_cursor', evidenceCursor);
  return apiRequest<StoryDetail>(`/api/v1/stories/${encodeURIComponent(storyId)}?${params.toString()}`);
}

/** Fetch the fixed 24-hour and seven-day baseline trends for selected sources. */
export function fetchNewsTrends(sourceIds: string[], limit = 25): Promise<TrendPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  sourceIds.forEach((id) => params.append('source_ids', id));
  return apiRequest<TrendPage>(`/api/v1/trends?${params.toString()}`);
}
