import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSmoothedText } from './use-smoothed-text';

describe('useSmoothedText', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('throttles updates and still shows the exact tail', () => {
    const text = 'word '.repeat(40).trim();
    const { result, rerender } = renderHook(({ t }) => useSmoothedText(t), { initialProps: { t: '' } });
    rerender({ t: text });
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current).toBe(text);
  });

  it('reveals at most one slice per 80 ms window', () => {
    const text = 'a'.repeat(400);
    const { result, rerender } = renderHook(({ t }) => useSmoothedText(t), { initialProps: { t: '' } });
    rerender({ t: text });
    const lengths = new Set<number>();
    for (let i = 0; i < 25; i += 1) {
      act(() => {
        vi.advanceTimersByTime(16);
      });
      lengths.add(result.current.length);
    }
    // 400 ms of frames: <= 6 distinct displayed lengths (initial + ~5 throttled steps).
    expect(lengths.size).toBeLessThanOrEqual(7);
  });
});
