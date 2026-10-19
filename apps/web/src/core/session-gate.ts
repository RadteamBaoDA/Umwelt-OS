/**
 * Exact public paths (no sub-paths): share-card images fetched by link previewers that cannot log in.
 * They render static content with no user data.
 */
const PUBLIC_EXACT = ['/opengraph-image', '/twitter-image'];

/** Name of the HTTP-only session cookie issued by the API (core/auth/dependencies.py SESSION_COOKIE). */
export const SESSION_COOKIE = 'bbd_session';

// Public entry points: the home page decides setup vs sign-in itself, and invite/setup flows run unauthenticated.
const PUBLIC_PREFIXES = ['/login', '/setup', '/invite', '/invitations', '/pick', '/_next', '/api', '/health'];

/**
 * Decides whether a page request should be redirected to /login.
 * Only checks cookie presence as a UX shortcut; the API remains the authority on session validity.
 * @param pathname Request path.
 * @param hasSession Whether the session cookie is present.
 */
export function needsLoginRedirect(pathname: string, hasSession: boolean): boolean {
  if (hasSession || pathname === '/' || PUBLIC_EXACT.includes(pathname)) return false;
  return !PUBLIC_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}
