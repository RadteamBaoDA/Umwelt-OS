import { describe, expect, it } from 'vitest';
import { formatCurrency, formatDate, formatDateTime, formatNumber, formatPercent, formatRelative } from '@/core/format';
import { formatDateTime as legacyFormatDateTime } from '@/core/i18n';
import { ApiError } from '@/core/api';
import { apiErrorMessageKey } from '@/core/api-failure-key';

describe('core/format', () => {
  it('matches the legacy datetime formatter output', () => {
    const iso = '2026-03-04T10:20:00Z';
    expect(formatDateTime(iso, 'en-us', 'UTC')).toBe(legacyFormatDateTime(iso, 'en-us', 'UTC'));
    expect(formatDateTime(iso, 'vi-vi', 'Asia/Ho_Chi_Minh')).toBe(legacyFormatDateTime(iso, 'vi-vi', 'Asia/Ho_Chi_Minh'));
  });

  it('normalises locale ids and formats numbers, currency and percent', () => {
    expect(formatNumber(1234.5, 'en-us')).toBe('1,234.5');
    expect(formatNumber(1234.5, 'vi-vi')).toBe('1.234,5');
    expect(formatCurrency(5, 'en-us', 'USD')).toBe('$5.00');
    expect(formatPercent(0.25, 'en-us')).toBe('25%');
    expect(formatDate('2026-03-04T10:20:00Z', 'en-us', 'UTC')).toBe('Mar 4, 2026');
  });

  it('picks the largest relative unit', () => {
    const now = Date.parse('2026-03-04T12:00:00Z');
    expect(formatRelative(now - 3 * 3_600_000, 'en-us', now)).toBe('3 hours ago');
    expect(formatRelative(now + 2 * 86_400_000, 'en-us', now)).toBe('in 2 days');
  });
});

describe('apiErrorMessageKey', () => {
  it('prefers the machine code, then status class, then generic', () => {
    expect(apiErrorMessageKey(new ApiError(400, 'x', { code: 'bad_thing' }))).toEqual({ namespace: 'errorCodes', key: 'bad_thing' });
    expect(apiErrorMessageKey(new ApiError(403, 'x'))).toEqual({ namespace: 'common', key: 'apiForbidden' });
    expect(apiErrorMessageKey(new Error('boom'))).toEqual({ namespace: 'common', key: 'apiRequestFailed' });
  });
});
