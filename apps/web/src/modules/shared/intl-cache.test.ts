import { describe, expect, it } from 'vitest';
import { getDateTimeFormat, getNumberFormat } from './intl-cache';

describe('intl-cache', () => {
  it('returns the same instance for equal locale/options regardless of key order', () => {
    expect(getNumberFormat('en-US', { maximumFractionDigits: 1, style: 'percent' })).toBe(getNumberFormat('en-US', { style: 'percent', maximumFractionDigits: 1 }));
    expect(getDateTimeFormat('en-US', { timeZone: 'UTC' })).toBe(getDateTimeFormat('en-US', { timeZone: 'UTC' }));
  });
  it('separates different locales and options, and formats like a fresh Intl instance', () => {
    expect(getNumberFormat('en-US')).not.toBe(getNumberFormat('vi-VN'));
    expect(getNumberFormat('en-US', { maximumFractionDigits: 0 }).format(1234.6)).toBe(new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(1234.6));
  });
});
