import { describe, expect, it } from 'vitest';
import { assignAuthorizationUrl } from '@/core/safe-url';
import { needsLoginRedirect } from '@/core/session-gate';

describe('needsLoginRedirect', () => {
  it('redirects protected pages without a session', () => {
    expect(needsLoginRedirect('/app', false)).toBe(true);
    expect(needsLoginRedirect('/settings/x', false)).toBe(true);
  });
  it('allows public paths and authenticated requests', () => {
    for (const p of ['/', '/login', '/setup', '/invite/abc', '/invitations', '/_next/x', '/api/v1/x']) expect(needsLoginRedirect(p, false)).toBe(false);
    expect(needsLoginRedirect('/app', true)).toBe(false);
    expect(needsLoginRedirect('/loginx', false)).toBe(true);
    for (const path of ['/privacy', '/terms', '/refunds', '/account-deletion']) expect(needsLoginRedirect(path, false)).toBe(false);
    expect(needsLoginRedirect('/privacy-private', false)).toBe(true);
  });
  it('treats only the exact share-image paths as public', () => {
    for (const p of ['/opengraph-image', '/twitter-image']) expect(needsLoginRedirect(p, false)).toBe(false);
    for (const p of ['/opengraph-image-x', '/opengraph-image/x', '/opengraph', '/twitter-images']) expect(needsLoginRedirect(p, false)).toBe(true);
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
