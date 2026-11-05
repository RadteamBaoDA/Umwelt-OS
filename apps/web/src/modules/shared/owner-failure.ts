import { ApiError } from '@/core/api';

/**
 * Picks the message key for a failed owner-scoped read or write: a 404 means the owner record is gone
 * (`missingKey`), 401/403 mean the session lost access (`ownerAccessDenied`), anything else is the generic `fallbackKey`.
 * Pure; the caller localizes the returned key.
 */
export function ownerFailureKey<M extends string, F extends string>(error: unknown, missingKey: M, fallbackKey: F): M | 'ownerAccessDenied' | F {
  if (error instanceof ApiError && error.status === 404) return missingKey;
  if (error instanceof ApiError && [401, 403].includes(error.status)) return 'ownerAccessDenied';
  return fallbackKey;
}
