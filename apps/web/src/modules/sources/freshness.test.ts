import { describe, expect, it } from 'vitest';
import { isStale, periodKind } from './freshness';

const now = new Date('2026-10-09T12:00:00Z');

describe('freshness', () => {
  it('is fresh within two intervals and stale beyond', () => {
    expect(isStale('2026-10-09T10:59:00Z', 30, now)).toBe(true);
    expect(isStale('2026-10-09T11:01:00Z', 30, now)).toBe(false);
    expect(isStale('2026-10-09T11:00:00Z', 60, now)).toBe(false);
  });
  it('never flags missing or invalid input', () => {
    expect(isStale(null, 30, now)).toBe(false);
    expect(isStale('nope', 30, now)).toBe(false);
    expect(isStale('2026-01-01T00:00:00Z', null, now)).toBe(false);
  });
  it('maps FX to reference date and World Bank to annual period', () => {
    expect(periodKind('frankfurter')).toBe('referenceDate');
    expect(periodKind('ecb')).toBe('referenceDate');
    expect(periodKind('world_bank')).toBe('annualPeriod');
    expect(periodKind('binance')).toBeNull();
    expect(periodKind(null)).toBeNull();
  });
});
