import { useTranslations } from 'next-intl';

/** Minimal translator shape: `has` tells whether a code has a catalog entry. */
export type ErrorCodeTranslator = { (key: string): string; has: (key: string) => boolean };

/** English fallback for codes without a catalog entry: "some_code" -> "Some code". */
function humanizeFallback(code: string): string {
  const words = code.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Returns the localized label for a machine error code, falling back to the humanized code for unknown ones. Pure. */
export function errorCodeLabel(t: ErrorCodeTranslator, code: string): string {
  return t.has(code) ? t(code) : humanizeFallback(code);
}

/** Hook returning a `(code) => label` function bound to the active locale's `errorCodes` catalog. */
export function useErrorCodeLabel(): (code: string) => string {
  const t = useTranslations('errorCodes') as unknown as ErrorCodeTranslator;
  return (code) => errorCodeLabel(t, code);
}
