import { describe, expect, it } from 'vitest';
import { formatTimeZoneLabel } from './i18n';

describe('formatTimeZoneLabel', () => {
  it('renders an offset label instead of the IANA id', () => {
    expect(formatTimeZoneLabel('Asia/Ho_Chi_Minh', 'en-us')).toBe('GMT+7');
  });
  it('falls back to the raw zone when Intl rejects it', () => {
    expect(formatTimeZoneLabel('Not/AZone', 'en-us')).toBe('Not/AZone');
  });
});
