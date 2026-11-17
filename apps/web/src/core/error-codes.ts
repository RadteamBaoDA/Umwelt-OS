import { useTranslations } from 'next-intl';

/**
 * Backend machine error codes that have a catalog entry. The catalog key of each code is the code itself
 * (locales/<locale>/errorCodes.json); the literal table keeps lookups typed, with no `t.has` probing.
 */
export const KNOWN_ERROR_CODES = [
  'invalid_credential',
  'provider_rejected',
  'provider_unavailable',
  'provider_response_invalid',
  'provider_rate_limited',
  'rate_limited',
  'provider_collection_failed',
  'provider_revoked',
  'quota_exceeded',
  'timeout',
  'network_error',
  'not_found',
  'source_not_found',
  'unauthorized',
  'forbidden',
  'configuration_invalid',
  'parse_failed',
  'executor_error',
  'collection_unsupported',
  'revision_changed',
  'admission_expired',
  'unknown',
] as const;

/** Machine error code that has a localized label. */
export type KnownErrorCode = (typeof KNOWN_ERROR_CODES)[number];

/** Minimal translator shape: resolves the label of a known code. */
export type ErrorCodeTranslator = (key: KnownErrorCode) => string;

/** English fallback for codes without a catalog entry: "some_code" -> "Some code". */
function humanizeFallback(code: string): string {
  const words = code.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** True when `code` has an entry in the `errorCodes` catalog. */
function isKnownErrorCode(code: string): code is KnownErrorCode {
  return (KNOWN_ERROR_CODES as readonly string[]).includes(code);
}

/** Returns the localized label for a machine error code, falling back to the humanized code for unknown ones. Pure. */
export function errorCodeLabel(t: ErrorCodeTranslator, code: string): string {
  return isKnownErrorCode(code) ? t(code) : humanizeFallback(code);
}

/** Hook returning a `(code) => label` function bound to the active locale's `errorCodes` catalog. */
export function useErrorCodeLabel(): (code: string) => string {
  const t = useTranslations('errorCodes');
  return (code) => errorCodeLabel(t, code);
}
