/**
 * Single source of truth for the app Content-Security-Policy. `proxy.ts` is the only layer that emits it (per request, with
 * a fresh nonce); `next.config.ts` and the TLS terminator (Caddyfile) must not set a CSP, because two CSP headers intersect.
 */

/** Request header carrying the per-request nonce from `proxy.ts` to server components (`app/layout.tsx`). */
export const NONCE_HEADER = 'x-nonce';

/** `<meta name>` the root layout renders so client code (generative-UI frame builder) can reuse the page nonce. */
export const NONCE_META_NAME = 'csp-nonce';

/**
 * Creates an unguessable per-request nonce (128 random bits, base64).
 *
 * @returns A base64 string safe to embed in a CSP source expression and an HTML attribute.
 */
export function createCspNonce(): string {
  return btoa(crypto.randomUUID());
}

/**
 * Builds the app CSP header value.
 *
 * `script-src` uses the nonce plus `'strict-dynamic'` (so scripts loaded by Next's nonced bootstrap work and the host
 * allowlist is ignored by modern browsers); `'self'` is a fallback for CSP2-only browsers. `'unsafe-eval'` is dev-only
 * (React refresh). `style-src` keeps `'unsafe-inline'` deliberately: React `style` props, Recharts, and Radix set inline
 * styles and styles cannot execute script. Everything else is `'self'`; data:/blob: images and blob: workers serve the globe.
 *
 * @param nonce - Per-request nonce from `createCspNonce`.
 * @param isDev - Allow `'unsafe-eval'` and websocket connects for the dev server.
 * @returns The `Content-Security-Policy` header value.
 */
export function buildAppCsp(nonce: string, isDev: boolean): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ''}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self'${isDev ? ' ws: wss:' : ''}`,
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join('; ');
}

/**
 * Reads the page's CSP nonce from the root layout's meta tag (browser only).
 *
 * @returns The nonce, or '' on the server, in tests, or when the page has no nonce CSP.
 */
export function readHostNonce(): string {
  if (typeof document === 'undefined') return '';
  return document.querySelector<HTMLMetaElement>(`meta[name="${NONCE_META_NAME}"]`)?.content ?? '';
}
