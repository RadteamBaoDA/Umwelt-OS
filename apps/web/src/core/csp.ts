/**
 * Single source of truth for the app Content-Security-Policy. `proxy.ts` is the only layer that emits it (per request, with
 * a fresh nonce); `next.config.ts` and the TLS terminator (Caddyfile) must not set a CSP, because two CSP headers intersect.
 */

/** Request header carrying the per-request nonce from `proxy.ts` to server components (`app/layout.tsx`). */
export const NONCE_HEADER = 'x-nonce';

/**
 * Creates an unguessable per-request nonce (128 random bits from `crypto.getRandomValues`, base64).
 *
 * @returns A base64 string safe to embed in a CSP source expression and an HTML attribute.
 */
export function createCspNonce(): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
}

/**
 * Exact origins the live-feed browser worker may fetch (public, CORS-capable sources). Keep in sync with
 * `modules/dashboard/live-feed/sources.ts`; a test asserts every browser source URL is covered here.
 *
 * Trade-off: these origins sit in the page-wide `connect-src` because narrowing them to the worker needs a CSP response
 * header on the worker script. Next 16 (Turbopack) serves that script as a content-hashed chunk under `/_next/static/chunks/`
 * (`turbopack-worker-<hash>.js`, wrapped by a bootstrap), so `headers()` in `next.config.ts` cannot target it by path, and a
 * wildcard header on every static chunk would be unverified for the bootstrap/blob path and still bypassed by a blob worker
 * that inherits the page policy. Page code is nonce-gated (`strict-dynamic`), so the extra origins widen only what already
 * trusted scripts could fetch. Revisit if Next exposes a stable worker URL.
 */
export const LIVE_FEED_ORIGINS = [
  'https://earthquake.usgs.gov',
  'https://eonet.gsfc.nasa.gov',
  'https://www.gdacs.org',
  'https://api.weather.gov',
  'https://hn.algolia.com',
  'https://services.nvd.nist.gov',
  'https://dev.to',
  'https://api.github.com',
  'https://api.coingecko.com',
  'https://api.alternative.me',
  'https://ll.thespacedevs.com',
  'https://www.freetogame.com',
  'https://www.gamerpower.com',
] as const;

/** Public basemap host used by the dashboard map (see `modules/dashboard/gadgets/world-map.tsx`). */
const OPENFREEMAP_ORIGIN = 'https://tiles.openfreemap.org';

/**
 * Builds the app CSP header value.
 *
 * `script-src` uses the nonce plus `'strict-dynamic'` (so scripts loaded by Next's nonced bootstrap work and the host
 * allowlist is ignored by modern browsers); `'self'` is a fallback for CSP2-only browsers. `'unsafe-eval'` is dev-only
 * (React refresh). `style-src` keeps `'unsafe-inline'` deliberately: React `style` props, Recharts, and Radix set inline
 * styles and styles cannot execute script. Everything else is `'self'`; data:/blob: images and blob: workers serve the MapLibre map. The MapLibre basemap is fetched
 * by the browser directly from OpenFreeMap (`OPENFREEMAP_ORIGIN`, no key, OSM data): style JSON, vector/raster tiles, glyphs and
 * sprites go through `connect-src`; `img-src` is allowed too for raster tiles/sprites decoded as images.
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
    `img-src 'self' data: blob: ${OPENFREEMAP_ORIGIN}`,
    "font-src 'self' data:",
    `connect-src 'self'${isDev ? ' ws: wss:' : ''} ${LIVE_FEED_ORIGINS.join(' ')} ${OPENFREEMAP_ORIGIN}`,
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join('; ');
}

/**
 * Strict fallback CSP for `/api/*`: those paths are excluded from the proxy.ts nonce policy and are served by the FastAPI
 * backend (rewrite), which sets no CSP itself except the media route (`default-src 'none'; sandbox`, same or stricter).
 * API responses are never documents, so nothing may load, run or frame. `/_next/*` static chunks are not documents either; no header there.
 */
export const API_FALLBACK_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox";
