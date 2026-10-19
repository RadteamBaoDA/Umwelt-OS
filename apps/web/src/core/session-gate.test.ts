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
  });
  it('treats only the exact share-image paths as public', () => {
    for (const p of ['/opengraph-image', '/twitter-image']) expect(needsLoginRedirect(p, false)).toBe(false);
    for (const p of ['/opengraph-image-x', '/opengraph-image/x', '/opengraph', '/twitter-images']) expect(needsLoginRedirect(p, false)).toBe(true);
  });
});

describe('assignAuthorizationUrl', () => {
  it('rejects non-http(s) URLs', () => {
    expect(() => assignAuthorizationUrl('javascript:alert(1)')).toThrow();
    expect(() => assignAuthorizationUrl('https://u:p@evil.test/')).toThrow();
  });
});
