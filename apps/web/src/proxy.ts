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
  // Skip static assets (anything with a file extension) and Next internals.
  matcher: ['/((?!_next/|api/|.*\.[^/]+$).*)'],
};
