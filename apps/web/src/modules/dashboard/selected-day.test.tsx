import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { addDays, isoDateInZone, SelectedDayProvider, useSelectedDay } from './selected-day';

const mocks = vi.hoisted(() => ({
  selectDay: vi.fn(),
  chat: { context: null as unknown, selectDay: undefined as unknown },
  prefs: { timezone: 'UTC' },
}));
mocks.chat.selectDay = mocks.selectDay;
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => mocks.chat }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => mocks.prefs }));

const wrapper = ({ children }: { children: ReactNode }) => <SelectedDayProvider>{children}</SelectedDayProvider>;

describe('selected-day helpers', () => {
  it('formats a date in the requested timezone', () => {
    const instant = new Date('2026-06-15T23:30:00Z');
    expect(isoDateInZone(instant, 'UTC')).toBe('2026-06-15');
    expect(isoDateInZone(instant, 'Asia/Ho_Chi_Minh')).toBe('2026-06-16');
  });

  it('adds days across month and year boundaries', () => {
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
  });
});

describe('useSelectedDay', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-06-15T12:00:00Z'));
    mocks.selectDay.mockReset();
    mocks.chat.context = null;
    window.history.replaceState(null, '', '/');
  });
  afterEach(() => vi.useRealTimers());

  it('throws outside the provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(() => renderHook(() => useSelectedDay())).toThrow('Selected day is unavailable');
    spy.mockRestore();
  });

  it('defaults to today in the display timezone', () => {
    const { result } = renderHook(() => useSelectedDay(), { wrapper });
    expect(result.current).toMatchObject({ date: '2026-06-15', today: '2026-06-15', timezone: 'UTC' });
  });

  it('adopts a valid ?date= from the URL after mount and ignores a malformed one', () => {
    window.history.replaceState(null, '', '/?date=2026-01-02');
    expect(renderHook(() => useSelectedDay(), { wrapper }).result.current.date).toBe('2026-01-02');
    window.history.replaceState(null, '', '/?date=yesterday');
    expect(renderHook(() => useSelectedDay(), { wrapper }).result.current.date).toBe('2026-06-15');
  });

  it('setDate stores the day and mirrors it into the URL, rejecting malformed input', () => {
    const { result } = renderHook(() => useSelectedDay(), { wrapper });
    act(() => result.current.setDate('2026-02-03'));
    expect(result.current.date).toBe('2026-02-03');
    expect(result.current.today).toBe('2026-06-15');
    expect(new URL(window.location.href).searchParams.get('date')).toBe('2026-02-03');
    act(() => result.current.setDate('not-a-date'));
    expect(result.current.date).toBe('2026-02-03');
  });

  it('moves an active day conversation to the newly selected day, but not other conversation kinds', () => {
    mocks.chat.context = { kind: 'day', date: '2026-06-15', timezone: 'UTC' };
    const { result } = renderHook(() => useSelectedDay(), { wrapper });
    expect(mocks.selectDay).not.toHaveBeenCalled();
    act(() => result.current.setDate('2026-02-03'));
    expect(mocks.selectDay).toHaveBeenCalledWith({ date: '2026-02-03', timezone: 'UTC' });

    mocks.selectDay.mockReset();
    mocks.chat.context = { kind: 'entity' };
    const other = renderHook(() => useSelectedDay(), { wrapper });
    act(() => other.result.current.setDate('2026-02-04'));
    expect(mocks.selectDay).not.toHaveBeenCalled();
  });
});
