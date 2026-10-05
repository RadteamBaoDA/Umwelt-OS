import { apiRequest, ApiError, csrfHeaders } from '@/core/api';
import type {
  Goal,
  GoalCreate,
  GoalFilter,
  GoalPage,
  GoalLookupResult,
  GoalUpdate,
  PlanAcceptanceResult,
  PlanProposal,
} from './types';

/** Fetches a paginated page of goals with optional status filtering. */
export function fetchGoals(filter?: GoalFilter): Promise<GoalPage> {
  const params = new URLSearchParams();
  if (filter?.status) params.set('status', filter.status);
  if (filter?.q) params.set('q', filter.q);
  if (filter?.limit) params.set('limit', String(filter.limit));
  if (filter?.cursor) params.set('cursor', filter.cursor);

  const qs = params.toString();
  return apiRequest<GoalPage>(`/api/v1/goals${qs ? `?${qs}` : ''}`);
}

/** Fetches a single goal by identifier. */
export function getGoal(id: string): Promise<Goal> {
  return apiRequest<Goal>(`/api/v1/goals/${encodeURIComponent(id)}`);
}

/** Reads explicitly selected owner goal IDs with four concurrent requests and explicit missing-state results. */
export async function fetchGoalsByIds(ids: string[]): Promise<GoalLookupResult[]> {
  const bounded = [...new Set(ids)].slice(0, 100);
  const results: GoalLookupResult[] = [];
  for (let offset = 0; offset < bounded.length; offset += 4) {
    const batch = bounded.slice(offset, offset + 4);
    const settled = await Promise.all(batch.map(async (id): Promise<GoalLookupResult> => {
      try { return { id, goal: await getGoal(id), missing: false }; }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) return { id, goal: null, missing: true };
        throw error;
      }
    }));
    results.push(...settled);
  }
  return results;
}

/** Creates a new strategic goal. */
export function createGoal(payload: GoalCreate, csrfToken: string): Promise<Goal> {
  return apiRequest<Goal>('/api/v1/goals', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/** Updates goal details, milestones, or progress under optimistic revisions. */
export function updateGoal(id: string, payload: GoalUpdate, csrfToken: string): Promise<Goal> {
  return apiRequest<Goal>(`/api/v1/goals/${id}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/** Deletes a goal by identifier. */
export function deleteGoal(id: string, expectedRevision: number, csrfToken: string): Promise<void> {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<void>(`/api/v1/goals/${encodeURIComponent(id)}?${query}`, {
    method: 'DELETE',
    headers: {
      ...csrfHeaders(csrfToken),
    },
  });
}

/** Atomically accepts a proposed plan and materializes tasks for a goal. */
export function acceptPlan(
  id: string,
  payload: PlanProposal,
  csrfToken: string,
): Promise<PlanAcceptanceResult> {
  return apiRequest<PlanAcceptanceResult>(`/api/v1/goals/${id}/accept-plan`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}
