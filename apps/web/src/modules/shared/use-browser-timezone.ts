import { useSyncExternalStore } from 'react';

/**
 * Reads the browser's IANA time zone without a post-mount state update.
 * @param fallback Zone used during server render and when the browser reports none (e.g. the configured display zone).
 * @returns The browser time zone on the client, otherwise `fallback`. The value never changes during a session.
 */
export function useBrowserTimezone(fallback: string): string {
  return useSyncExternalStore(() => () => {}, () => Intl.DateTimeFormat().resolvedOptions().timeZone || fallback, () => fallback);
}
