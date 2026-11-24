import { ApiError } from '@/core/api';

/** Error whose message is already localized for the user (safe to display verbatim). */
export class LocalizedError extends Error {}

/** Catalog keys (in the `chat` namespace) that a selection failure can resolve to. */
export type SelectionErrorKey =
  'context.selectionLocalOnly' | 'context.selectionUnavailable' | 'context.selectionTooLarge' | 'sourceScope.unavailable';

/** Catalog keys a classified chat failure can resolve to, plus whatever fallback the caller passed. */
export type ChatFailureKey = SelectionErrorKey | 'sessionExpired' | 'error.conflict' | 'error.rateLimited' | 'error.service';

/** Server machine codes for selection failures mapped to their localized catalog keys. */
export const SELECTION_ERROR_KEYS: Record<string, SelectionErrorKey> = {
  selection_local_only: 'context.selectionLocalOnly',
  selection_unavailable: 'context.selectionUnavailable',
  selection_too_large: 'context.selectionTooLarge',
  selection_invalid: 'context.selectionTooLarge',
  // 409 when none of the `@`-picked sources survives server-side validation (see the chat source scope).
  source_scope_unavailable: 'sourceScope.unavailable',
};

/**
 * Maps a chat failure to a `chat` catalog key. Server `detail` text is English-only and never shown:
 * selection codes win, then HTTP status class, then the caller's fallback. The raw error goes to the console.
 *
 * @param err - Thrown value from a chat request.
 * @param fallback - Catalog key used for non-API errors and unclassified statuses.
 * @returns Catalog key to translate with the `chat` namespace.
 */
export function chatFailureKey<F extends string>(err: unknown, fallback: F): F | ChatFailureKey {
  if (!(err instanceof ApiError)) return fallback;
  console.warn('Chat request failed', err.status, err.code, err.message);
  if (err.code && Object.hasOwn(SELECTION_ERROR_KEYS, err.code)) return SELECTION_ERROR_KEYS[err.code];
  if (err.status === 401) return 'sessionExpired';
  if (err.status === 409) return 'error.conflict';
  if (err.status === 429) return 'error.rateLimited';
  if (err.status >= 500) return 'error.service';
  return fallback;
}
