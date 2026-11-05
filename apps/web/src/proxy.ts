import { NextResponse, type NextRequest } from 'next/server';
import { buildAppCsp, createCspNonce, NONCE_HEADER } from '@/core/csp';
import { needsLoginRedirect, SESSION_COOKIE } from '@/core/session-gate';

/**
 * Redirects page requests without a session cookie to /login, and otherwise forwards the request with a fresh
 * per-request CSP nonce. The CSP is set on the request (Next reads the nonce from it to tag its own bootstrap scripts),
 * on `x-nonce` (read by the root layout), and on the response. This is the only place the app CSP is emitted.
 */
export function proxy(request: NextRequest) {
  const nonce = createCspNonce();
  const csp = buildAppCsp(nonce, process.env.NODE_ENV !== 'production');
  if (needsLoginRedirect(request.nextUrl.pathname, request.cookies.has(SESSION_COOKIE))) {
    const redirect = NextResponse.redirect(new URL('/login', request.url));
    redirect.headers.set('Content-Security-Policy', csp);
    return redirect;
  }
  const headers = new Headers(request.headers);
  headers.set(NONCE_HEADER, nonce);
  headers.set('Content-Security-Policy', csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  // Next 16 loses the escaped dot from negative-lookahead matchers, skipping every page path.
  matcher: ['/((?!_next/|api/).*)'],
};
