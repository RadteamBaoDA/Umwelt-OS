import { describe, expect, it } from 'vitest';
import { humanizeCode } from './source-list';

describe('humanizeCode', () => {
  it('turns machine codes into sentence-case words', () => {
    expect(humanizeCode('invalid_credential')).toBe('Invalid credential');
    expect(humanizeCode('provider-unauthorized')).toBe('Provider unauthorized');
    expect(humanizeCode('')).toBe('');
  });
});
