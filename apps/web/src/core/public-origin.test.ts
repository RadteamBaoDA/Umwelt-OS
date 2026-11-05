import { describe, expect, it } from 'vitest';
import { getPublicOrigin, parsePublicOrigin } from './public-origin';

const h = (o: Record<string, string>) => ({ get: (k: string) => o[k] ?? null });

describe('parsePublicOrigin', () => {
  it('normalizes valid http(s) origins', () => {
    expect(parsePublicOrigin('http://localhost:38701/path?x=1')).toBe('http://localhost:38701');
    expect(parsePublicOrigin(' https://umwelt.example.com ')).toBe('https://umwelt.example.com');
  });
  it('rejects missing, malformed and non-http values', () => {
    for (const v of [undefined, null, '', 'localhost:3000', 'javascript:alert(1)', 'ftp://x.example']) {
      expect(parsePublicOrigin(v)).toBeNull();
    }
  });
});

describe('getPublicOrigin', () => {
  it('prefers PUBLIC_ORIGIN over headers', () => {
    expect(getPublicOrigin(h({ host: 'evil.test' }), { PUBLIC_ORIGIN: 'http://localhost:38701' })).toBe('http://localhost:38701');
  });
  it('falls back to validated forwarded headers when unset', () => {
    expect(getPublicOrigin(h({ 'x-forwarded-proto': 'https', host: 'a.example.com' }), {})).toBe('https://a.example.com');
  });
  it('rejects CSP-injecting hosts and missing data', () => {
    expect(getPublicOrigin(h({ 'x-forwarded-proto': 'https', host: "a.com; script-src *" }), {})).toBeNull();
    expect(getPublicOrigin(h({ host: 'a.com' }), {})).toBeNull();
    expect(getPublicOrigin(undefined, {})).toBeNull();
  });
});
