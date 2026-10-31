import { describe, expect, it } from 'vitest';
import { cleanExcerpt } from './excerpt';

describe('cleanExcerpt', () => {
  it('drops release label, repeated title and inline headings', () => {
    expect(cleanExcerpt('v16.5.0-canary.34', 'Tag: v16.5.0-canary.34 ### Credits Thanks to **all**')).toBe('Credits Thanks to all');
  });
  it('strips markdown links, bullets and backticks', () => {
    expect(cleanExcerpt('T', '### Misc Changes\n- fix `x` [doc](http://a) ![i](y)')).toBe('Misc Changes fix x doc');
  });
  it('drops a title copy ignoring case and punctuation', () => {
    expect(cleanExcerpt('CVE-2026-1234', 'cve 2026 1234: Buffer overflow')).toBe('Buffer overflow');
  });
  it('drops a repeated identifier-like first title token', () => {
    expect(cleanExcerpt('CVE-2026-91920 MEDIUM', 'CVE-2026-91920 A flaw in parser')).toBe('A flaw in parser');
  });
  it('returns null when empty or only the title', () => {
    expect(cleanExcerpt('Title', '  ')).toBeNull();
    expect(cleanExcerpt('Title', 'Title')).toBeNull();
    expect(cleanExcerpt('Title', null)).toBeNull();
  });
});
