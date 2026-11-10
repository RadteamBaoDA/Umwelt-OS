import { ApiError } from '@/core/api';

/** Error whose message is already localized for the user (safe to display verbatim). */
export class LocalizedError extends Error {}

/** Server machine codes for selection failures mapped to their localized catalog keys. */
export const SELECTION_ERROR_KEYS: Record<string, string> = {
  selection_local_only: 'contextSelectionLocalOnly',
  selection_unavailable: 'contextSelectionUnavailable',
  selection_too_large: 'contextSelectionTooLarge',
  selection_invalid: 'contextSelectionTooLarge',
};

/**
 * Maps a chat failure to a `chat` catalog key. Server `detail` text is English-only and never shown:
 * selection codes win, then HTTP status class, then the caller's fallback. The raw error goes to the console.
 *
 * @param err - Thrown value from a chat request.
 * @param fallback - Catalog key used for non-API errors and unclassified statuses.
 * @returns Catalog key to translate with the `chat` namespace.
 */
export function chatFailureKey(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback;
  console.warn('Chat request failed', err.status, err.code, err.message);
  if (err.code && Object.hasOwn(SELECTION_ERROR_KEYS, err.code)) return SELECTION_ERROR_KEYS[err.code];
  if (err.status === 401) return 'sessionExpired';
  if (err.status === 409) return 'errorConflict';
  if (err.status === 429) return 'errorRateLimited';
  if (err.status >= 500) return 'errorService';
  return fallback;
}
