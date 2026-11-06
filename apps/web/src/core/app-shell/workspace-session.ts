'use client';

import { createContext, useContext } from 'react';
import type { Session } from '@/core/app-shell/use-shell-session';

/**
 * Session context lives apart from the workspace shell so light consumers (e.g. the account barrel pulled into the
 * public login/setup pages) can read it without importing the whole shell and every module behind it.
 */
export const SessionContext = createContext<Session | null>(null);

/** Returns the authenticated workspace session, including its CSRF token; throws when used outside the session provider. */
export function useWorkspaceSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error('Workspace session is unavailable');
  return session;
}
