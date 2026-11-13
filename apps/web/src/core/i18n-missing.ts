/**
 * E2E-only missing-translation reporting for next-intl.
 *
 * When `NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N === '1'` (inlined by Next at BUILD time, so it must be a build arg of the
 * disposable e2e web image; default off, production behaviour is untouched) every message next-intl cannot resolve
 * emits `console.error('[i18n-missing] <namespace>.<key>')` and the key itself is rendered. The Playwright fixture in
 * `tests/e2e/fixtures.ts` fails any test whose page console contains that marker (browser) and the same line appears in
 * the web server log (SSR, via `i18n-request.ts`). With the flag off these helpers return empty/pass-through handlers,
 * leaving next-intl's defaults (and the English fallback used by the providers) exactly as before.
 */

/** next-intl `getMessageFallback` signature. */
export type MessageFallback = (info: { namespace?: string; key: string; error: unknown }) => string;

/** Marker the e2e fixture greps for in console output. */
export const I18N_MISSING_MARKER = '[i18n-missing]';

/** True when the build was made with the e2e missing-key detector enabled. */
function detectorEnabled(): boolean {
  return process.env.NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N === '1';
}

/**
 * Build the `onError` / `getMessageFallback` props for a next-intl provider or request config.
 * @param fallback Existing fallback (e.g. the English catalog lookup); used for the rendered text. Without one the
 *   fallback renders `namespace.key`, which is next-intl's own default.
 * @returns `{}` or just the fallback when the detector is off; when on, a handler pair that logs the marker once per
 *   missing message (the fallback hook, not `onError`, reports, so the default error log is suppressed rather than doubled).
 */
export function missingKeyHandlers(fallback?: (namespace: string | undefined, key: string) => string): {
  onError?: () => void;
  getMessageFallback?: MessageFallback;
} {
  const render = (namespace: string | undefined, key: string) => fallback?.(namespace, key) ?? [namespace, key].filter(Boolean).join('.');
  if (!detectorEnabled()) return fallback ? { getMessageFallback: ({ namespace, key }) => render(namespace, key) } : {};
  return {
    onError: () => undefined,
    getMessageFallback: ({ namespace, key }) => {
      console.error(`${I18N_MISSING_MARKER} ${[namespace, key].filter(Boolean).join('.')}`);
      return render(namespace, key);
    },
  };
}
