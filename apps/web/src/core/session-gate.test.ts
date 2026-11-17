import { describe, expect, it } from 'vitest';
import { assignAuthorizationUrl } from '@/core/safe-url';
import { isPublicPath, needsLoginRedirect } from '@/core/session-gate';

describe('needsLoginRedirect', () => {
  it('redirects protected pages without a session', () => {
    expect(needsLoginRedirect('/dashboard', false)).toBe(true);
    expect(needsLoginRedirect('/settings/x', false)).toBe(true);
  });
  it('allows public paths and authenticated requests', () => {
    for (const p of ['/', '/login', '/setup', '/invite/abc', '/invitations', '/_next/x', '/api/v1/x'])
      expect(needsLoginRedirect(p, false)).toBe(false);
    expect(needsLoginRedirect('/dashboard', true)).toBe(false);
    expect(needsLoginRedirect('/loginx', false)).toBe(true);
    for (const path of ['/privacy', '/terms', '/refunds', '/account-deletion']) expect(needsLoginRedirect(path, false)).toBe(false);
    expect(needsLoginRedirect('/privacy-private', false)).toBe(true);
  });
  it('treats only the exact share-image paths as public', () => {
    for (const p of ['/opengraph-image', '/twitter-image']) expect(needsLoginRedirect(p, false)).toBe(false);
    for (const p of ['/opengraph-image-x', '/opengraph-image/x', '/opengraph', '/twitter-images'])
      expect(needsLoginRedirect(p, false)).toBe(true);
  });
  it('keeps public static assets available when the proxy matches all page paths', () => {
    for (const p of ['/icon.svg', '/vendor/genui/index.js', '/fonts/umwelt.woff2']) expect(needsLoginRedirect(p, false)).toBe(false);
  });
});

describe('assignAuthorizationUrl', () => {
  it('rejects non-http(s) URLs', () => {
    expect(() => assignAuthorizationUrl('javascript:alert(1)')).toThrow();
    expect(() => assignAuthorizationUrl('https://u:p@evil.test/')).toThrow();
  });
});

describe('isPublicPath', () => {
  it('treats public entry points and their sub-paths as public, and workspace routes as private', () => {
    for (const path of ['/', '/login', '/invite/abc', '/invitations/accept', '/terms', '/opengraph-image']) {
      expect(isPublicPath(path)).toBe(true);
    }
    for (const path of ['/dashboard', '/chat', '/settings/models', '/loginx']) {
      expect(isPublicPath(path)).toBe(false);
    }
  });
});
