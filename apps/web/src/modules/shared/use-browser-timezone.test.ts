import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useBrowserTimezone } from './use-browser-timezone';

afterEach(() => vi.restoreAllMocks());

describe('useBrowserTimezone', () => {
  it('returns the browser zone when one is reported', () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({
      timeZone: 'Asia/Ho_Chi_Minh',
    } as Intl.ResolvedDateTimeFormatOptions);
    expect(renderHook(() => useBrowserTimezone('UTC')).result.current).toBe('Asia/Ho_Chi_Minh');
  });

  it('falls back to the supplied zone when the browser reports none', () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({ timeZone: '' } as Intl.ResolvedDateTimeFormatOptions);
    expect(renderHook(() => useBrowserTimezone('Europe/Paris')).result.current).toBe('Europe/Paris');
  });
});
