import { describe, expect, it } from 'vitest';
import { computeFreshnessTick, jitterPx } from './freshness-strip';

const NOW = Date.parse('2026-10-30T12:00:00Z');
const base = { id: 's', name: 'S', last_success_at: null, last_error_at: null, last_error_code: null, collection_error_code: null, next_due_at: null };

describe('computeFreshnessTick', () => {
  it('places a 12h-old success mid-axis, stale without cadence', () => {
    const tick = computeFreshnessTick({ ...base, last_success_at: '2026-10-30T00:00:00Z' }, NOW);
    expect(tick.position).toBeCloseTo(50);
    expect(tick.status).toBe('stale');
  });
  it('is fresh at the right edge when just collected', () => {
    const tick = computeFreshnessTick({ ...base, last_success_at: '2026-10-30T12:00:00Z' }, NOW);
    expect(tick).toMatchObject({ position: 100, status: 'fresh', clamped: false });
  });
  it('uses next_due_at as cadence when present', () => {
    const src = { ...base, last_success_at: '2026-10-30T06:00:00Z' };
    expect(computeFreshnessTick({ ...src, next_due_at: '2026-10-30T18:00:00Z' }, NOW).status).toBe('fresh');
    expect(computeFreshnessTick({ ...src, next_due_at: '2026-10-30T09:00:00Z' }, NOW).status).toBe('stale');
  });
  it('clamps older than 24h and never-succeeded to the left edge', () => {
    expect(computeFreshnessTick({ ...base, last_success_at: '2026-10-28T00:00:00Z' }, NOW)).toMatchObject({ position: 0, clamped: true });
    expect(computeFreshnessTick(base, NOW)).toMatchObject({ position: 0, clamped: true, lastSuccessMs: null });
  });
  it('is failing when the error is newer than the success', () => {
    const tick = computeFreshnessTick({ ...base, last_success_at: '2026-10-30T10:00:00Z', last_error_at: '2026-10-30T11:00:00Z', last_error_code: 'E1' }, NOW);
    expect(tick).toMatchObject({ status: 'failing', errorCode: 'E1' });
  });
});

describe('jitterPx', () => {
  it('offsets ticks sharing a position so they stay visible', () => {
    const t = computeFreshnessTick({ ...base, last_success_at: '2026-10-30T12:00:00Z' }, NOW);
    expect([0, 1, 2, 3].map((i) => jitterPx([t, t, t, t], i))).toEqual([0, 2, -2, 0]);
  });
});
