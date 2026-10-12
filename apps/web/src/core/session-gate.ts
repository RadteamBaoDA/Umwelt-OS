/** Name of the HTTP-only session cookie issued by the API (core/auth/dependencies.py SESSION_COOKIE). */
export const SESSION_COOKIE = 'bbd_session';

// Public entry points: the home page decides setup vs sign-in itself, and invite/setup flows run unauthenticated.
const PUBLIC_PREFIXES = ['/login', '/setup', '/invite', '/invitations', '/_next', '/api', '/health'];

/**
 * Decides whether a page request should be redirected to /login.
 * Only checks cookie presence as a UX shortcut; the API remains the authority on session validity.
 * @param pathname Request path.
 * @param hasSession Whether the session cookie is present.
 */
export function needsLoginRedirect(pathname: string, hasSession: boolean): boolean {
  if (hasSession || pathname === '/') return false;
  return !PUBLIC_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}
