import { describe, expect, it } from 'vitest';
import { axisValueFormatter, nearestRange, rangeLookbackDays, staleInfo } from './chart-range';

describe('chart range', () => {
  it('maps lookbacks to the nearest chip', () => {
    expect(nearestRange(1)).toBe('24h');
    expect(nearestRange(7)).toBe('7D');
    expect(nearestRange(10)).toBe('7D');
    expect(nearestRange(30)).toBe('30D');
    expect(nearestRange(120)).toBe('90D');
    expect(nearestRange(400)).toBe('1Y');
    expect(nearestRange(3660)).toBe('ALL');
    expect(nearestRange(0)).toBe('24h');
  });
  it('maps chips to lookbacks within the API maximum', () => {
    expect(rangeLookbackDays('7D')).toBe(7);
    expect(rangeLookbackDays('ALL')).toBe(3660);
  });
  it('formats axis ticks by spacing', () => {
    const big = axisValueFormatter(66000, 72000, 'en-US');
    expect(big(72000)).toBe('72K');
    expect(big(69000)).toBe('69K');
    const fine = axisValueFormatter(1.08, 1.1, 'en-US');
    expect(fine(1.09)).toBe('1.09');
  });
  it('derives stale info and returns null without facts', () => {
    const now = Date.parse('2026-11-12T12:00:00Z');
    expect(staleInfo(undefined, now)).toBeNull();
    expect(staleInfo({ last_success_at: null, next_due_at: null }, now)).toBeNull();
    expect(staleInfo({ last_success_at: '2026-11-12T10:00:00Z', next_due_at: '2026-11-12T13:20:00Z' }, now)).toEqual({
      ago: '2h',
      nextDueMs: Date.parse('2026-11-12T13:20:00Z'),
    });
    expect(staleInfo({ last_success_at: '2026-11-12T10:00:00Z' }, now)).toEqual({ ago: '2h', nextDueMs: null });
  });
});
