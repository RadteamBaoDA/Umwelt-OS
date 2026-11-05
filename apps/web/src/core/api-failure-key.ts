import { ApiError } from '@/core/api';

export type ApiFailureKey = 'unauthorized' | 'forbidden' | 'conflict' | 'serviceUnavailable' | 'requestFailed';

/** Maps a recognized API failure to a stable message key, or returns null when it is not recognized. */
export function apiFailureKey(error: unknown): ApiFailureKey | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 401) return 'unauthorized';
  if (error.status === 403) return 'forbidden';
  if (error.status === 409) return 'conflict';
  if (error.status >= 500) return 'serviceUnavailable';
  return 'requestFailed';
}

const COMMON_FAILURE_KEYS: Record<ApiFailureKey, string> = {
  unauthorized: 'apiUnauthorized',
  forbidden: 'apiForbidden',
  conflict: 'apiConflict',
  serviceUnavailable: 'apiServiceUnavailable',
  requestFailed: 'apiRequestFailed',
};

/**
 * Maps any thrown value to an i18n message reference for display. A machine `code` on an ApiError wins
 * (`errorCodes` namespace; render with `errorCodeLabel` so unknown codes fall back to humanized text), then the
 * status class and finally the generic request-failed copy, both in the `common` namespace.
 */
export function apiErrorMessageKey(error: unknown): { namespace: 'errorCodes' | 'common'; key: string } {
  if (error instanceof ApiError && error.code) return { namespace: 'errorCodes', key: error.code };
  return { namespace: 'common', key: COMMON_FAILURE_KEYS[apiFailureKey(error) ?? 'requestFailed'] };
}
