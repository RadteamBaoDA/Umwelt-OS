'use client';

import { queryOptions, useQuery } from '@tanstack/react-query';
import { apiRequest, retrySessionConflict } from '@/core/api';

/** Current browser session as returned by `GET /api/v1/auth/session`. */
export type Session = { authenticated: true; csrfToken: string; isInstanceAdmin: boolean };

/** The one query key for the session; `setQueryData` callers (CSRF rotation, refresh events) must use it too. */
export const SESSION_KEY = ['session'] as const;

/**
 * Shared query options for the session. The server answers 409 ("retry the request") when concurrent tabs or requests
 * hold its NOWAIT owner lock while the CSRF token rotates, so a transient conflict is retried briefly; a 401 is final.
 */
export const sessionQuery = queryOptions({
  queryKey: SESSION_KEY,
  queryFn: () => apiRequest<Session>('/api/v1/auth/session'),
  retry: retrySessionConflict,
  retryDelay: (attempt) => 150 * (attempt + 1),
});

/**
 * Read the current session through the shared cache entry; every caller gets the same type and retry policy.
 * @returns The TanStack query result for the session.
 */
export function useSession() {
  return useQuery(sessionQuery);
}
