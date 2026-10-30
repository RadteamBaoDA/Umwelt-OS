import { describe, expect, it } from 'vitest';
import { formatYTick } from './recharts-wrapper';

describe('formatYTick', () => {
  it('keeps 5 ticks over a narrow range distinct', () => {
    const span = 77484 - 77373;
    const labels = [0, 1, 2, 3, 4].map((i) => formatYTick(77373 + (span * i) / 4, 'en-us', span));
    expect(new Set(labels).size).toBe(5);
  });

  it('keeps compact notation for large spans', () => {
    expect(formatYTick(1_200_000, 'en-us', 1_000_000)).toBe('1.2M');
  });
});
