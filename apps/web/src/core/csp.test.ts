import { describe, expect, it } from 'vitest';
import { API_FALLBACK_CSP, buildAppCsp, createCspNonce } from './csp';

describe('app CSP', () => {
  it('uses the nonce with strict-dynamic and no script unsafe-inline in production', () => {
    const csp = buildAppCsp('abc123', false);
    const script = csp.split('; ').find((d) => d.startsWith('script-src'))!;
    expect(script).toBe("script-src 'self' 'nonce-abc123' 'strict-dynamic'");
    expect(csp).not.toContain('unsafe-eval');
    expect(csp).not.toContain('ws:');
    expect(csp).toContain("style-src 'self' 'unsafe-inline'");
    expect(csp).toContain("frame-ancestors 'none'");
  });

  it('adds eval and websockets only in dev', () => {
    const csp = buildAppCsp('n', true);
    expect(csp).toContain("'strict-dynamic' 'unsafe-eval'");
    expect(csp).toContain("connect-src 'self' ws: wss:");
  });

  it('creates distinct header-safe nonces', () => {
    const a = createCspNonce();
    expect(a).not.toBe(createCspNonce());
    expect(a).toMatch(/^[A-Za-z0-9+/=]+$/);
    expect(atob(a)).toHaveLength(16);
  });
});

describe('API fallback CSP', () => {
  it('is fully restrictive and sandboxed', () => {
    expect(API_FALLBACK_CSP).toBe("default-src 'none'; frame-ancestors 'none'; sandbox");
  });
});
