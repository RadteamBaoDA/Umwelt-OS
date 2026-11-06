import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useWeatherUnit, WEATHER_UNIT_KEY } from './weather-model';

describe('useWeatherUnit', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it('defaults to Celsius', () => {
    expect(renderHook(() => useWeatherUnit()).result.current[0]).toBe('c');
  });

  it('reads a persisted Fahrenheit choice', () => {
    localStorage.setItem(WEATHER_UNIT_KEY, 'f');
    expect(renderHook(() => useWeatherUnit()).result.current[0]).toBe('f');
  });

  it('persists the new unit and updates every mounted consumer', () => {
    const a = renderHook(() => useWeatherUnit());
    const b = renderHook(() => useWeatherUnit());
    act(() => a.result.current[1]('f'));
    expect(localStorage.getItem(WEATHER_UNIT_KEY)).toBe('f');
    expect(a.result.current[0]).toBe('f');
    expect(b.result.current[0]).toBe('f');
  });

  it('keeps the old unit when storage writes are blocked', () => {
    const { result } = renderHook(() => useWeatherUnit());
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    act(() => result.current[1]('f'));
    expect(result.current[0]).toBe('c');
  });

  it('falls back to Celsius when storage reads are blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(renderHook(() => useWeatherUnit()).result.current[0]).toBe('c');
  });
});
