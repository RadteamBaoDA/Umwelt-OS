import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePendingStage } from './use-pending-stage';

describe('usePendingStage', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('advances to "still translating" at 8 s and "waiting in queue" at 30 s while pending', () => {
    const { result } = renderHook(({ pending }) => usePendingStage(pending), { initialProps: { pending: true } });
    expect(result.current).toBe(0);
    act(() => vi.advanceTimersByTime(7_999));
    expect(result.current).toBe(0);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(1);
    act(() => vi.advanceTimersByTime(21_999));
    expect(result.current).toBe(1);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(2);
  });

  it('resets to stage 0 when pending turns false and starts over on the next pending run', () => {
    const { result, rerender } = renderHook(({ pending }) => usePendingStage(pending), { initialProps: { pending: true } });
    act(() => vi.advanceTimersByTime(8_000));
    expect(result.current).toBe(1);
    rerender({ pending: false });
    expect(result.current).toBe(0);
    rerender({ pending: true });
    act(() => vi.advanceTimersByTime(7_000));
    expect(result.current).toBe(0);
  });

  it('schedules no timers while not pending', () => {
    renderHook(() => usePendingStage(false));
    expect(vi.getTimerCount()).toBe(0);
  });
});
