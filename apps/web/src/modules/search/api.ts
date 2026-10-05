import { apiRequest } from '@/core/api';
import type { Goal } from '@/modules/goals/types';
import type { Milestone } from '@/modules/goals/types';
import type { Task, TaskPage } from '@/modules/tasks/types';

/** Carries backend-scored document provenance, excerpt, entity references, and the cited source revision. */
export type SearchHit = {
  document_id: string;
  document_version_id: string;
  version_number: number;
  chunk_id: string;
  title: string;
  excerpt: string;
  source: { id: string; name: string; type: string };
  observed_at: string | null;
  published_at: string | null;
  content_type: string | null;
  score: number;
  entity_refs: string[];
  citation: { sourceType: 'document'; sourceId: string; documentId: string; chunkId: string; title: string; url: string | null; observedAt: string | null; quote: string };
};

export type EntitySearchHit = { id: string; type: string; name: string | null; revision: number; aliases: { id: string; alias: string; confirmed: boolean }[] };
export type EntitySearchResponse = { items: EntitySearchHit[]; next_cursor: string | null };

export type SearchResponse = { items: SearchHit[]; next_cursor: string | null; effective_mode: 'lexical' | 'hybrid'; warnings: string[] };
export type SearchIndexStatus = { run_id: string | null; status: string; model_id: string | null; dimensions: number | null; indexed_items: number; failed_items: number };
export type SearchFilters = { source_ids: string[]; date_from: string | null; date_to: string | null; content_types: string[] };

/** Matches the task owner's read DTO without adding search-only ranking fields. */
export type TaskSearchHit = Task;

/** Extends the goal owner's read projection with its linked entity and milestone task references. */
export type GoalSearchHit = Omit<Goal, 'milestones'> & {
  entity_ids: string[];
  milestones: (Milestone & { task_id: string | null })[];
};

/** Carries the goal owner's read projection and independent cursor without search ranking metadata. */
export type GoalSearchPage = { items: GoalSearchHit[]; next_cursor: string | null; total?: number | null };

/** Describes the legacy global endpoint's independent domain continuations and response-local total. */
export type GlobalSearchResponse = {
  documents: SearchHit[];
  tasks: TaskSearchHit[];
  goals: GoalSearchHit[];
  total: number;
  document_next_cursor: string | null;
  task_next_cursor: string | null;
  goal_next_cursor: string | null;
};

/** Searches documents with the supplied query, filters, lexical or hybrid mode, and optional cursor. */
export function searchDocuments(query: string, filters: SearchFilters, mode: 'lexical' | 'hybrid', cursor?: string) {
  return apiRequest<SearchResponse>('/api/v1/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, filters, mode, limit: 20, cursor: cursor ?? null }),
  });
}

/** Searches entities by query with optional cursor pagination. */
export function searchEntities(query: string, cursor?: string) {
  const params = new URLSearchParams({ limit: '20', q: query });
  if (cursor) params.set('cursor', cursor);
  return apiRequest<EntitySearchResponse>(`/api/v1/entities?${params}`);
}

/** Searches tasks by query with optional cursor pagination. */
export function searchTasks(query: string, cursor?: string) {
  const params = new URLSearchParams({ limit: '20', q: query });
  if (cursor) params.set('cursor', cursor);
  return apiRequest<TaskPage>(`/api/v1/tasks?${params}`);
}

/** Searches goals by query with optional cursor pagination. */
export function searchGoals(query: string, cursor?: string) {
  const params = new URLSearchParams({ limit: '20', q: query });
  if (cursor) params.set('cursor', cursor);
  return apiRequest<GoalSearchPage>(`/api/v1/goals?${params}`);
}

/** Runs unified global search across documents, tasks, and goals. */
export function searchGlobal(query: string, limit = 20) {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return apiRequest<GlobalSearchResponse>(`/api/v1/search/global?${params}`);
}

/** Fetches the current search-index status. */
export function getSearchIndexStatus() { return apiRequest<SearchIndexStatus>('/api/v1/search/index'); }

