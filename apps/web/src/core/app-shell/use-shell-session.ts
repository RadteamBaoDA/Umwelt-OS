'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef } from 'react';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { syncServerAccent } from '@/core/accent';
import type { ModuleAvailability } from '@/core/module-registry';
import { fetchOwnerPreferences, OWNER_PREFERENCES_KEY } from '@/core/preferences';
import { useDisplayPreferences } from '@/core/query-provider';
import { SESSION_KEY, useSession, type Session } from '@/core/session';

export type { Session } from '@/core/session';

/**
 * Module-scope latch: true from the owner's logout request until the next successful session read.
 * It outlives the auth-keyed remount of the shell, whose fresh refs would otherwise report the post-logout 401
 * as `?reason=expired`.
 */
let ownerSignOutInFlight = false;

/**
 * Registers the window listeners that keep the shell's session in sync: `umwelt:unauthorized` ends the session as
 * expired and `umwelt:session-refreshed` writes the refreshed session into the query cache.
 */
function useSessionEvents(endSession: (expired?: boolean) => void): void {
  const client = useQueryClient();
  useEffect(() => {
    /** Routes an unauthorized event through the shared session-ending handler. */
    const unauthorized = () => endSession(true);
    /** Updates the cached session from the detail of a session-refresh event. */
    const refreshed = (event: Event) => client.setQueryData(SESSION_KEY, (event as CustomEvent<Session>).detail);
    window.addEventListener('umwelt:unauthorized', unauthorized);
    window.addEventListener('umwelt:session-refreshed', refreshed);
    return () => {
      window.removeEventListener('umwelt:unauthorized', unauthorized);
      window.removeEventListener('umwelt:session-refreshed', refreshed);
    };
  }, [client, endSession]);
}

/**
 * Owns the shell's server state: session (with 409-conflict retry), module availability, owner preferences,
 * API health polling, the idempotent `endSession` teardown and the logout mutation.
 *
 * Ordering invariants: `endSession` runs at most once per shell instance, announces `umwelt:auth-ending` before the query
 * cache is cleared, and chooses `/login?reason=expired` only when neither this shell nor the module latch saw the
 * owner sign out. A 401 from the session query ends the session as expired.
 *
 * @param resetOverlays Closes shell-owned dialogs/menus; called during `endSession` and must be referentially stable.
 */
export function useShellSession(resetOverlays: () => void) {
  const router = useRouter();
  const client = useQueryClient();
  const { authGeneration: generation, isCurrentGeneration, confirmPreferences, endAuthSession } = useDisplayPreferences();
  const sessionEndedRef = useRef(false);
  /** Set while the owner's own logout is in flight: the server has revoked the session, so 401s are expected. */
  const signingOutRef = useRef(false);
  const session = useSession();
  const moduleAvailability = useQuery({
    queryKey: ['module-lifecycle'],
    queryFn: () => apiRequest<ModuleAvailability>('/api/v1/settings/modules'),
    refetchOnWindowFocus: true,
  });
  const preferences = useQuery({
    queryKey: OWNER_PREFERENCES_KEY,
    enabled: Boolean(session.data),
    queryFn: async ({ signal }) => {
      const value = await fetchOwnerPreferences(signal);
      if (!isCurrentGeneration(generation)) throw new Error('Stale preference response');
      return value;
    },
  });
  const apiHealth = useQuery({ queryKey: ['shell-api-health'], queryFn: () => apiRequest<unknown>('/health'), refetchInterval: 30_000 });

  useEffect(() => {
    if (session.data && preferences.data && isCurrentGeneration(generation)) {
      confirmPreferences(preferences.data, generation);
      syncServerAccent(preferences.data.accent);
    }
  }, [session.data, preferences.data, generation, confirmPreferences, isCurrentGeneration]);

  /** Clears authenticated workspace state and ends the current client session. */
  const endSession = useCallback(
    (expired = false) => {
      if (sessionEndedRef.current) return;
      sessionEndedRef.current = true;
      window.dispatchEvent(new Event('umwelt:auth-ending'));
      void client.cancelQueries();
      client.clear();
      endAuthSession();
      resetOverlays();
      // Auth-keyed remounts create a fresh shell with signingOutRef=false; the module latch keeps the owner's
      // sign-out from being re-reported as an expiry by that shell's own 401.
      router.replace(expired && !signingOutRef.current && !ownerSignOutInFlight ? '/login?reason=expired' : '/login');
    },
    [client, endAuthSession, resetOverlays, router],
  );

  useSessionEvents(endSession);

  useEffect(() => {
    if (session.error instanceof ApiError && session.error.status === 401) endSession(true);
  }, [endSession, session.error]);

  useEffect(() => {
    // A successful session read proves a new login: later 401s are genuine expiries again.
    // The ended shell is excluded: it can still re-emit its stale cached session after client.clear().
    if (session.data && !sessionEndedRef.current) ownerSignOutInFlight = false;
  }, [session.data]);

  const logout = useMutation({
    mutationFn: () => {
      signingOutRef.current = true;
      ownerSignOutInFlight = true;
      return apiRequest<void>('/api/v1/auth/logout', {
        method: 'POST',
        headers: csrfHeaders(session.data?.csrfToken ?? ''),
      });
    },
    onSuccess: () => endSession(),
    onError: () => {
      signingOutRef.current = false;
      ownerSignOutInFlight = false;
    },
  });

  /** Loads owner preferences for the dialog retry; returns null on request failure, abort, or stale auth generation. */
  const reloadPreferences = useCallback(
    async (signal: AbortSignal) => {
      try {
        const value = await fetchOwnerPreferences(signal);
        return signal.aborted || !isCurrentGeneration(generation) ? null : value;
      } catch {
        return null;
      }
    },
    [isCurrentGeneration, generation],
  );

  return { session, moduleAvailability, preferences, apiHealth, logout, reloadPreferences, generation };
}
