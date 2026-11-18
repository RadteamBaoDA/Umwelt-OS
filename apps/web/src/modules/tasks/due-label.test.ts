import { describe, expect, it } from 'vitest';
import { dueLabel } from './due-label';

const text = { overdue: (d: number) => `overdue ${d}d`, today: 'today', none: 'no due date' };

describe('dueLabel', () => {
  it('labels past, current and missing days', () => {
    expect(dueLabel('2026-11-16', '2026-11-18', 'en-US', text)).toBe('overdue 2d');
    expect(dueLabel('2026-11-18', '2026-11-18', 'en-US', text)).toBe('today');
    expect(dueLabel(null, '2026-11-18', 'en-US', text)).toBe('no due date');
  });

  it('uses a short weekday within a week and month/day beyond', () => {
    expect(dueLabel('2026-11-20', '2026-11-18', 'en-US', text)).toBe('Fri');
    expect(dueLabel('2026-11-24', '2026-11-18', 'en-US', text)).toBe('Tue');
    expect(dueLabel('2026-11-25', '2026-11-18', 'en-US', text)).toBe('Nov 25');
  });
});
