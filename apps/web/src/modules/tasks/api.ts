import { apiRequest, ApiError, csrfHeaders } from '@/core/api';
import type { Task, TaskCreate, TaskFilter, TaskLookupResult, TaskPage, TaskUpdate } from './types';

/** Fetches a paginated page of tasks matching the supplied filters. */
export function fetchTasks(filter?: TaskFilter): Promise<TaskPage> {
  const params = new URLSearchParams();
  if (filter?.view) params.set('view', filter.view);
  if (filter?.status) params.set('status', filter.status);
  if (filter?.goal_id) params.set('goal_id', filter.goal_id);
  if (filter?.entity_id) params.set('entity_id', filter.entity_id);
  if (filter?.due_date_from) params.set('due_date_from', filter.due_date_from);
  if (filter?.due_date_to) params.set('due_date_to', filter.due_date_to);
  if (filter?.due_at_from) params.set('due_at_from', filter.due_at_from);
  if (filter?.due_at_to) params.set('due_at_to', filter.due_at_to);
  if (filter?.q) params.set('q', filter.q);
  if (filter?.timezone) params.set('timezone', filter.timezone);
  if (filter?.limit) params.set('limit', String(filter.limit));
  if (filter?.cursor) params.set('cursor', filter.cursor);

  const qs = params.toString();
  return apiRequest<TaskPage>(`/api/v1/tasks${qs ? `?${qs}` : ''}`);
}

/** Fetches a single task by identifier. */
export function getTask(id: string): Promise<Task> {
  return apiRequest<Task>(`/api/v1/tasks/${encodeURIComponent(id)}`);
}

/** Reads explicitly selected owner task IDs with four concurrent requests and explicit missing-state results. */
export async function fetchTasksByIds(ids: string[]): Promise<TaskLookupResult[]> {
  const bounded = [...new Set(ids)].slice(0, 100);
  const results: TaskLookupResult[] = [];
  for (let offset = 0; offset < bounded.length; offset += 4) {
    const batch = bounded.slice(offset, offset + 4);
    const settled = await Promise.all(batch.map(async (id): Promise<TaskLookupResult> => {
      try { return { id, task: await getTask(id), missing: false }; }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) return { id, task: null, missing: true };
        throw error;
      }
    }));
    results.push(...settled);
  }
  return results;
}

/** Creates a new task under the owner account. */
export function createTask(payload: TaskCreate, csrfToken: string): Promise<Task> {
  return apiRequest<Task>('/api/v1/tasks', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/** Updates an existing task with optimistic revision controls. */
export function updateTask(id: string, payload: TaskUpdate, csrfToken: string): Promise<Task> {
  return apiRequest<Task>(`/api/v1/tasks/${id}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/** Deletes an owner task by identifier. */
export function deleteTask(id: string, expectedRevision: number, csrfToken: string): Promise<void> {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<void>(`/api/v1/tasks/${encodeURIComponent(id)}?${query}`, {
    method: 'DELETE',
    headers: {
      ...csrfHeaders(csrfToken),
    },
  });
}
