/**
 * Parent-side image loader for sandboxed widgets (`bridge/image-loader.ts`).
 * The sandbox CSP only allows `img-src data: blob:` and a null-origin frame cannot carry the session
 * cookie, so the host fetches same-origin media-proxy URLs on the widget's behalf and hands back a
 * `blob:` URL. Everything here is fail-closed: any violation throws and the widget gets an error.
 */

/** Maximum accepted image body in bytes (matches the proxy cap). */
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

/** Maximum accepted request URL length. */
export const MAX_IMAGE_URL_LENGTH = 2048;

const PROXY_PATH = '/api/v1/media/proxy';
const TILE_PATH = /^\/api\/v1\/media\/tile\/\d{1,2}\/\d{1,7}\/\d{1,7}$/;

/**
 * Resolves a widget-supplied URL to a canonical same-origin media-proxy URL.
 * Only `/api/v1/media/proxy?token=...` and `/api/v1/media/tile/{z}/{x}/{y}` (no query) pass; absolute
 * URLs must share the host origin. Protocol-relative, credentialed and fragment-bearing URLs are rejected.
 *
 * @param raw - Untrusted URL string from the widget.
 * @param origin - Host origin (`window.location.origin`).
 * @returns Absolute same-origin URL string, or `null` when not permitted.
 */
export function resolveMediaUrl(raw: unknown, origin: string): string | null {
  if (typeof raw !== 'string' || !raw || raw.length > MAX_IMAGE_URL_LENGTH || raw.startsWith('//')) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(raw, origin);
  } catch {
    return null;
  }
  if (parsed.origin !== origin || parsed.username || parsed.password || parsed.hash) {
    return null;
  }
  if (parsed.pathname === PROXY_PATH) {
    const keys = [...parsed.searchParams.keys()];
    if (keys.length !== 1 || keys[0] !== 'token' || !parsed.searchParams.get('token')) {
      return null;
    }
    return parsed.href;
  }
  if (TILE_PATH.test(parsed.pathname) && !parsed.search) {
    return parsed.href;
  }
  return null;
}

/**
 * Fetches a validated media URL with credentials and returns the image as a Blob.
 *
 * @param url - Absolute URL already approved by `resolveMediaUrl`.
 * @param fetchImpl - Fetch implementation (injectable for tests).
 * @returns Image blob no larger than `MAX_IMAGE_BYTES`.
 * @throws Error when the response is not OK, not a raster `image/*`, or over the size cap.
 */
export async function fetchImageBlob(url: string, fetchImpl: typeof fetch = fetch): Promise<Blob> {
  const response = await fetchImpl(url, { credentials: 'same-origin', redirect: 'error' });
  if (!response.ok) {
    throw new Error('Image request failed');
  }
  const type = (response.headers.get('content-type') ?? '').split(';')[0].trim().toLowerCase();
  // SVG can embed script/external refs; raster formats only.
  if (!type.startsWith('image/') || type === 'image/svg+xml') {
    throw new Error('Response is not an image');
  }
  const declared = Number(response.headers.get('content-length') ?? 0);
  if (declared > MAX_IMAGE_BYTES) {
    throw new Error('Image too large');
  }
  const blob = await response.blob();
  if (blob.size > MAX_IMAGE_BYTES) {
    throw new Error('Image too large');
  }
  return blob.type === type ? blob : new Blob([blob], { type });
}
