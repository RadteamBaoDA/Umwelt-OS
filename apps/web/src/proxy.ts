import { NextResponse, type NextRequest } from 'next/server';
import { needsLoginRedirect, SESSION_COOKIE } from '@/core/session-gate';

/** Redirects page requests without a session cookie to /login before any protected route renders. */
export function proxy(request: NextRequest) {
  if (needsLoginRedirect(request.nextUrl.pathname, request.cookies.has(SESSION_COOKIE))) {
    return NextResponse.redirect(new URL('/login', request.url));
  }
  return NextResponse.next();
}

export const config = {
  // Next 16 loses the escaped dot from negative-lookahead matchers, skipping every page path.
  matcher: ['/((?!_next/|api/).*)'],
};
