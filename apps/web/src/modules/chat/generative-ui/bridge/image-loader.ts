/**
 * Parent-side image loader for sandboxed widgets (`bridge/image-loader.ts`).
 * The sandbox CSP only allows `img-src data: blob:` (we only use `data:`) and a null-origin frame cannot carry the session
 * cookie, so the host fetches same-origin media-proxy URLs on the widget's behalf and hands back a
 * `data:` URL (a host-origin `blob:` URL is not reliably loadable from an opaque-origin frame). Everything here is fail-closed: any violation throws and the widget gets an error.
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

/**
 * Encodes a raster image blob as a `data:` URL. Bytes are read once and base64-encoded in chunks so large
 * (up to `MAX_IMAGE_BYTES`) images do not overflow the argument limit of `String.fromCharCode`.
 * The cap is re-checked on the actual bytes here, independent of any declared length.
 *
 * @param blob - Image blob already validated by `fetchImageBlob` (raster `image/*`).
 * @returns `data:image/<type>;base64,...` string (never SVG).
 * @throws Error when the bytes exceed `MAX_IMAGE_BYTES` or the type is not a raster image.
 */
export async function blobToDataUrl(blob: Blob): Promise<string> {
  const type = blob.type.toLowerCase();
  if (!/^image\/[a-z0-9.+-]+$/.test(type) || type === 'image/svg+xml') {
    throw new Error('Response is not an image');
  }
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (bytes.length > MAX_IMAGE_BYTES) {
    throw new Error('Image too large');
  }
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return `data:${type};base64,${btoa(binary)}`;
}
