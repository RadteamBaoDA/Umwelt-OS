/** Matches a bare `hostname[:port]` (DNS name, IPv4 or bracketed IPv6); no `;`, space, `/` or `,` can pass, so it cannot inject CSP directives. */
const HOST_PATTERN = /^(?:[a-z0-9]([a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$/i;

/**
 * Parses a configured public origin. Only `http:`/`https:` URLs are accepted; the result is `URL.origin`, which drops any
 * path, query, credentials and default port.
 *
 * @param value - Raw setting such as `process.env.PUBLIC_ORIGIN`.
 * @returns The normalized origin, or `null` when the value is missing or invalid.
 */
export function parsePublicOrigin(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.origin : null;
  } catch {
    return null;
  }
}

/**
 * Resolves the browser-facing origin of this deployment. The Next standalone server sees its bind address
 * (`http://0.0.0.0:3000`) in `request.url`, so the origin must come from configuration. Order:
 * 1. `PUBLIC_ORIGIN` (the same setting the API checks writes against);
 * 2. only when unset, `X-Forwarded-Proto` + `X-Forwarded-Host`/`Host` of the request, each validated strictly
 *    (`http|https`, `hostname[:port]` regex) because the value lands in a CSP header;
 * 3. `null` (callers then omit the origin-dependent source).
 *
 * @param headers - Request headers used only for the fallback.
 * @param env - Environment source (injectable for tests).
 * @returns Normalized origin or `null`.
 */
export function getPublicOrigin(
  headers?: Pick<Headers, 'get'>,
  env: Record<string, string | undefined> = process.env,
): string | null {
  const configured = parsePublicOrigin(env.PUBLIC_ORIGIN);
  if (configured || !headers) return configured;
  // Take the first hop when a proxy chain appends values.
  const proto = headers.get('x-forwarded-proto')?.split(',')[0]?.trim().toLowerCase();
  const host = (headers.get('x-forwarded-host') ?? headers.get('host'))?.split(',')[0]?.trim();
  if ((proto !== 'http' && proto !== 'https') || !host || !HOST_PATTERN.test(host)) return null;
  return parsePublicOrigin(`${proto}://${host}`);
}
